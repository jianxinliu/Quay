"""Usage guide and best practices for the agent (attached to the result on the
session's first tool call).

**Why not rely solely on MCP instructions**: `instructions` are rendered by the client at
connect time, and clients handle it very differently (some truncate it, some collapse it,
some only show it once at the very top of the system prompt) — in practice agents often
never see it, or see it once and forget. So on top of keeping `instructions`, we prepare a
full guide here that `server._FirstCallGuide` middleware attaches to the result on the
**first successful tool call of each session** — that's when the agent is about to act on
it, so the odds it actually gets read are highest, and it's only sent once per session so
it doesn't keep eating context. The agent can also re-read it anytime via `usage_guide()`.

Content strategy: **organize by scenario**, not by tool listing. Misuse is rarely caused by
not knowing a tool exists — it's caused by not knowing which combination fits the
situation. Pulling an entire large table into context, doing aggregation work that should
happen in SQL, or changing data without leaving a rollback trail all come from that gap.
"""

from __future__ import annotations

USAGE_GUIDE = """\
# Quay Database Service · Usage Guide & Best Practices

(This guide is sent once per session; re-read it anytime with `usage_guide()`.)

## 0. Do this first

`begin_session(title, note)` — declare what this session is called and what it's for.
Every SQL statement you run afterwards will be grouped under this session on the backend
so a human can trace it back, and you can find it again yourself with `list_sessions` /
`session_history`. Not calling it still works, but the backend will only see an opaque
session id.

## 1. Four hard rules

1. **Reads and writes are strictly separated.** Read-only queries go through `query`
   (accepts only SELECT/SHOW/DESCRIBE/EXPLAIN, uses the read-only account); any data
   change goes through `execute` (generates an approval ticket, executed with the writer
   account only after a human approves). Putting a write statement into `query` will just
   be rejected — don't try it.
2. **Large results do not belong in context.** Aggregate/narrow it down in SQL first; if
   you truly need the full dataset, dump it to a file (`export_table`) or into the local
   sandbox (`analysis_*`), and bring back only the conclusion. Pulling tens of thousands of
   rows into context is slow, expensive, and rarely helps.
3. **Context has a budget.** This session's cumulative returned volume has a cap; once hit,
   further data fetches are rejected and you must **stop and ask the user** whether to
   continue these token-costly queries — only after they agree should you call
   `allow_more_results` to get another allowance. Don't try to work around it by retrying —
   it's cheaper to only fetch what you need in the first place than to get blocked.
4. **Think about rollback before changing data.** You decide whether this change is worth
   leaving a rollback trail for; if it is, use `query` first to read the old values, then
   pass them into `execute(..., rollback_note=...)`. The approver sees it immediately, and
   you can retrieve it later with `session_history` to reconstruct a rollback statement.
   Leave it blank for changes that don't matter.

## 2. Scenario → which tools to use

### Exploring: I don't know what's there
- What projects/connections exist → `list_projects` → `list_connections`
- What tables exist → `list_tables` (for a connection with no default database, call
  `list_databases` first)
- Other PostgreSQL databases → `list_server_databases` lists the databases, then pass
  `pg_database=<name>` to other tools (a PG connection only queries one database at a
  time; the `database` parameter means *schema* for PG)
- Table structure → `describe_table` (columns/types/indexes/primary key — most
  context-efficient)
- How indexes are built, whether it's partitioned, charset/defaults/comments verbatim →
  `table_ddl` (accepts a comma-separated list of tables)
- What the data looks like → `sample_rows(limit=10)`. **Don't `SELECT *` a large table
  just to "take a look".**

### Health check: is this database healthy
→ `db_checkup`. Returns a structured diagnostic report in one call — **do not** run
round after round of `query` to probe it manually (every round costs a round trip and
context, and it's easy to miss a metric you didn't know to check).
- Coverage: connection usage, cache hit rate, slow/long-running queries, lock waits and
  deadlocks, idle transactions, replication lag, top-5 largest tables (varies by engine:
  16 checks for MySQL / 16 for PostgreSQL / 8 for ClickHouse / 5 for SQLite).
- The views were chosen to avoid most permission gates: MySQL long-query/lock-wait checks
  use performance_schema (no PROCESS privilege needed), PG connection-usage/replication-
  slot/statistics views are visible to read-only accounts; checks that genuinely lack
  permission are summarized in the report's `privileges` field as ready-to-copy GRANT
  statements — treat `unknown` as "not yet confirmed", not "healthy". Ask the DBA to grant
  the listed privileges if you need those checks.
- Each item has a `status` (ok / info / warn / critical / unknown) + a human-readable
  `value` + interpretation guidance; `overall` is the most severe status among them —
  **look at overall and the summary first, then drill into the warn/critical items.**
- **`unknown` means "not measured", not "healthy"**: the common cause is insufficient
  read-only-account privileges (e.g. PG without `pg_monitor` can't see other sessions,
  MySQL can't see the full process list) — the report explains why. Don't treat unknown as
  evidence of health, and don't try to work around it yourself — just tell the user and let
  them decide whether to grant more privileges.
- Read-only, no approval needed. For PG, pass `pg_database` to pick which database to
  check.

### Querying: I need data
- Ordinary queries → `query`. Large tables **must** have a WHERE or a LIMIT.
- Aggregation/summaries → do the aggregation in SQL (GROUP BY / SUM / COUNT); don't pull
  the raw rows back and count them yourself.
- Unsure of the volume → run `SELECT COUNT(*) ... WHERE ...` first, then decide how to
  fetch.
- Slow → run `EXPLAIN` via `query` to check for a full table scan
  (access_type=ALL/table), and adjust the WHERE clause against `describe_table`'s index
  list.

### Result too large
Follow this order, don't skip steps:
1. **Aggregate if you can** — compute the answer in SQL.
2. **Need the full dataset but don't need to "read" it** → `export_table`
   (CSV/JSON/Markdown/XLSX). It returns a `download_url` — **download it to the target
   location with code, never read the file content into context.**
3. **Need multi-step processing / a cross-source JOIN** → the analysis workbench (local
   DuckDB sandbox): `analysis_import` snapshots each source's query result into a
   workspace dataset → `analysis_sql` freely JOINs/aggregates/creates VIEWs inside the
   workspace (no approval needed for the sandbox), bringing back only the small final
   result. **This is the correct way to do a cross-database JOIN** — not pulling both
   sides back and joining them yourself.

### Modifying data
1. First `query` the rows that will be changed to capture their old values (when you want
   a rollback trail).
2. `execute(sql, reason=..., rollback_note="before: id=1001 status=2; rollback: UPDATE ...")`.
3. The return value has an `approval_url` — **give it to the user to open and approve.**
   Once approved, this same call auto-executes and returns `status=executed` — the user
   doesn't need to come back and tell you "I approved it".
4. If the wait times out, `status=approval_required` is returned; remind the user, then
   call `wait_for_change(change_id)` to keep waiting. **Don't loop on
   `get_change_status` yourself.**
5. Migration-style changes (e.g. ALTER + a backfill UPDATE) can be submitted as multiple
   statements in one call (semicolon-separated) — one approval covers the whole batch,
   executed statement by statement in the same transaction.

### Getting production data onto local
- **Just the table structure** (rebuild an empty table locally that mirrors production) →
  `sync_table_ddl`, can take several tables at once.
- **A small sample of real data** to run against → `sync_table(data="append", where=...,
  limit=...)`. It's for **sampling**, not migration: both row count and byte size have
  server-side hard caps, and you'll be told honestly if it truncated.
- **A full backup** → use `sync_table_ddl` to build the structure + `export_table` to dump
  the data to a file, then download it with code. **Do not** expect `sync_table` to move
  the full dataset.
- The target cannot be a prod connection. Targeting local/dev needs no approval; targeting
  staging goes through the approval flow.

### Looking back and rolling back
- What have I done before → `list_sessions` (filter by date/keyword/project/connection;
  `writes_only=True, status="ok"` = sessions that actually changed data)
- What exactly did that session do → `session_history(session_id)`. Returns a compact
  column set by default; ask for `fields="sql,detail"` explicitly to get the raw SQL and
  error details.
- To roll back → pull the `rollback_note` you wrote at the time from `session_history`,
  build the rollback statement from it, and **submit the rollback through `execute` just
  like any other write — it still requires approval.**

### Persisting work
For analyses you run repeatedly → `save_workflow` to save it as a re-runnable workflow,
then `run_workflow` to re-run it with one call (re-pulls the source data → runs each step
→ returns the status of each). See `analysis_workspaces` for the list of available ones.

## 3. SQL-writing conventions

- **Time zones**: this service does not pin a fixed database session time zone —
  `@@session.time_zone` inherits whatever each database is set to (could be UTC+8, could
  be UTC). Whenever you use a time-zone-dependent function such as `FROM_UNIXTIME` /
  `UNIX_TIMESTAMP` / `NOW` / `CURDATE` / `DATE`, run `SELECT @@session.time_zone` first to
  confirm it, and **never apply an offset on top of it** (if the session is already UTC+8
  and you manually add +28800 too, that's +16h — grouping by day will bleed evening data
  into the next day).
- **Grouping epoch-second columns by day**: compute `day_idx` with pure arithmetic —
  `FLOOR((ts+offset)/86400)` — then derive the date from it with
  `DATE_ADD('1970-01-01', INTERVAL day_idx DAY)`, sidestepping implicit time-zone
  conversion.
- **Don't break indexes**: don't wrap an indexed column in a function or arithmetic
  (`DATE(ts)`, `FROM_UNIXTIME(ts)`, `ts+1` on the left side of a WHERE clause) — convert
  the constant side instead, and use a range comparison (`ts >= start AND ts < end`).
- **Large tables must be narrowed**: a WHERE or a LIMIT, at minimum one of the two.

## 4. What results look like

`query` / `sample_rows` return **compact TSV text** (not JSON, to save tokens): a top `#`
metadata line + `# types:` column types, then a header row with column names, then data
rows, tab-separated, `\\N` for NULL, big integers transmitted as strings (to avoid
precision loss).

Results have **two hard caps**: row count (default 1000) and a character budget (default
roughly 12k tokens). `truncated=true` in the metadata means you didn't get everything —
**don't just resend the same query to "get the full set"** — narrow it with WHERE/LIMIT,
switch to aggregation, or push the computation into the analysis workbench.

There's also a **session-level quota**: as the session's cumulative returned volume
approaches the cap, an extra `# budget: ...` line is appended to the result as a warning;
once it's exceeded, further fetches are rejected (`[result_budget_exceeded]`). At that
point, **ask the user first** whether to continue, and only after they agree call
`allow_more_results(reason="user confirmed: ...")` to get one more allowance. Grants show
up on the backend dashboard — don't call it without having actually asked the user.

## 5. How to read errors

Every error carries a bracketed category prefix — use it to decide the next step, and
don't blindly resend the same SQL:

| Prefix | Meaning & next step |
| --- | --- |
| `[sql_syntax_error]` | Syntax error (confirmed by the target database), you must rewrite the SQL |
| `[table_not_found]` / `[column_not_found]` | Check the name with `list_tables` / `describe_table` |
| `[permission_denied]` / `[readonly_violation]` | The read-only account can't write; data changes go through the `execute` approval flow |
| `[query_timeout]` | Narrow the range, or switch to aggregation / the analysis workbench |
| `[connection_unavailable]` | The connection is temporarily down and the backend is auto-reconnecting; retry after the suggested number of seconds |
| `[connection_exhausted]` | Repeated reconnect attempts have failed (still retrying) — please ask the user to take a look at the backend |
| `[result_budget_exceeded]` | This session has pulled back too much data; stop and ask the user, then call `allow_more_results` once they agree |

## 6. Boundaries: things you can't do — don't try to work around them

- **Redis is not exposed to the agent** — it can only be operated by a human through the
  admin backend.
- **Connection and credential management are not exposed as tools** — you cannot change
  connection configuration, and you can't obtain any account passwords.
- **You cannot sync data into a prod connection**; writes against prod are always forced
  through approval.
- All operations are audited (who, when, which connection, what SQL, what result) — this
  is by design.
"""
