"""分析 workflow 存储：工作区取数配方 + 多语句 SQL 脚本，可一键重跑。

定义（JSON 存 SQLite）：
- sources: 保存时从工作区 provenance 自动收集（每个数据集怎么拉的）
- script:  编辑器里的多语句 SQL（分号分隔），运行时逐条执行
- graph:   可视化 DAG（节点+连线，见 compile_graph）；有 graph 时运行以图编译为准
运行 = 按 sources 重拉数据 → 逐条执行 script → 最后一个有结果集的语句作为输出预览。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis_workflow (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    workspace   TEXT NOT NULL,
    script      TEXT NOT NULL,
    sources     TEXT NOT NULL DEFAULT '[]',   -- JSON：取数配方列表
    chart       TEXT NOT NULL DEFAULT '',     -- JSON：图表配置（type/x/y/agg），空 = 无
    graph       TEXT NOT NULL DEFAULT '',     -- JSON：DAG 画布（nodes/edges），空 = 纯脚本
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""


class WorkflowError(Exception):
    """workflow 操作失败。message 面向使用者。"""


@dataclass
class Workflow:
    id: int
    name: str
    workspace: str
    script: str
    sources: list[dict]
    chart: dict | None
    graph: dict | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "workspace": self.workspace,
                "script": self.script, "sources": self.sources, "chart": self.chart,
                "graph": self.graph,
                "created_at": self.created_at, "updated_at": self.updated_at}


class WorkflowStore:
    def __init__(self, db_path: str | Path):
        db_path = Path(db_path)
        if str(db_path) != ":memory:":
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(analysis_workflow)")}
            for missing in ("chart", "graph"):  # 老库升级
                if missing not in cols:
                    self._conn.execute(
                        f"ALTER TABLE analysis_workflow ADD COLUMN {missing} TEXT NOT NULL DEFAULT ''")
            self._conn.commit()

    def save(self, name: str, workspace: str, script: str, sources: list[dict],
             chart: dict | None = None, graph: dict | None = None) -> Workflow:
        name = (name or "").strip()
        if not name:
            raise WorkflowError("workflow 名称不能为空")
        if not (script or "").strip() and not graph:
            raise WorkflowError("workflow 脚本不能为空")
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute(
                "INSERT INTO analysis_workflow"
                " (name, workspace, script, sources, chart, graph, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(name) DO UPDATE SET workspace = excluded.workspace,"
                " script = excluded.script, sources = excluded.sources,"
                " chart = excluded.chart, graph = excluded.graph, updated_at = excluded.updated_at",
                (name, workspace, script, json.dumps(sources, ensure_ascii=False),
                 json.dumps(chart, ensure_ascii=False) if chart else "",
                 json.dumps(graph, ensure_ascii=False) if graph else "", now, now))
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM analysis_workflow WHERE name = ?", (name,)).fetchone()
        return _row(row)

    def get(self, name: str) -> Workflow:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM analysis_workflow WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise WorkflowError(f"workflow {name!r} 不存在")
        return _row(row)

    def list(self) -> list[Workflow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM analysis_workflow ORDER BY updated_at DESC").fetchall()
        return [_row(r) for r in rows]

    def delete(self, name: str) -> None:
        with self._lock:
            cur = self._conn.execute("DELETE FROM analysis_workflow WHERE name = ?", (name,))
            if cur.rowcount == 0:
                raise WorkflowError(f"workflow {name!r} 不存在")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _row(row: sqlite3.Row) -> Workflow:
    return Workflow(id=row["id"], name=row["name"], workspace=row["workspace"],
                    script=row["script"], sources=json.loads(row["sources"] or "[]"),
                    chart=json.loads(row["chart"]) if row["chart"] else None,
                    graph=json.loads(row["graph"]) if row["graph"] else None,
                    created_at=row["created_at"], updated_at=row["updated_at"])


# ---------- DAG 画布编译 ----------
#
# graph = {"nodes": [...], "edges": [{"from": id, "to": id, "port": "in|left|right"}]}
# 节点 = {"id", "type", "name", "x", "y", "cfg": {...}}，type ∈：
#   source    cfg: {conn: "project/connection", sql, limit?, schema?}   → 导入为数据集
#   file      cfg: {path}                                               → 文件导入为数据集
#   filter    cfg: {where}          输入 1（in）
#   join      cfg: {kind, on, select?, ports_n?}  输入 N（端口 in_1..in_N，SQL 别名 a/b/c/...；老图 left/right 兼容为 in_1/in_2）
#   aggregate cfg: {group, aggs}    输入 1
#   sql       cfg: {sql}            输入任意（直接按上游节点名引用）
#   output    cfg: {order_by?, limit?}  输入 1，终点（最多一个）
# 非 source/file 节点编译为 CREATE OR REPLACE VIEW "节点名" AS ...，按拓扑序执行；
# 中间结果都是工作区里的视图，可单独预览。

_NODE_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")


def _q(name: str) -> str:
    return '"' + name + '"'


def compile_graph(graph: dict) -> dict:
    """把 DAG 编译为可执行计划：{sources, steps: [{node, name, sql}], output_sql}。

    校验：节点名合法且唯一、输入口连接完整、无环。失败抛 WorkflowError（面向使用者）。
    """
    nodes = {n.get("id"): n for n in (graph.get("nodes") or [])}
    if not nodes:
        raise WorkflowError("流程为空：请先添加节点")
    seen_names = set()
    for n in nodes.values():
        name = (n.get("name") or "").strip()
        if not _NODE_NAME_RE.match(name):
            raise WorkflowError(f"节点名 {name!r} 不合法（字母开头，仅字母/数字/下划线）")
        if name.lower() in seen_names:
            raise WorkflowError(f"节点名 {name!r} 重复")
        seen_names.add(name.lower())

    # 入边：to_id -> {port: from_id}
    inputs: dict[str, dict[str, str]] = {nid: {} for nid in nodes}
    for e in graph.get("edges") or []:
        f, t = e.get("from"), e.get("to")
        if f not in nodes or t not in nodes:
            continue  # 悬空边（节点已删）直接忽略
        inputs[t][e.get("port") or "in"] = f

    def _one_input(n: dict) -> dict:
        up = inputs[n["id"]].get("in")
        if not up:
            raise WorkflowError(f"节点「{n['name']}」缺少输入连线")
        return nodes[up]

    # Kahn 拓扑排序
    indeg = {nid: len(inputs[nid]) for nid in nodes}
    order, queue = [], sorted([nid for nid, d in indeg.items() if d == 0])
    downstream: dict[str, list[str]] = {nid: [] for nid in nodes}
    for t, ports in inputs.items():
        for f in ports.values():
            downstream[f].append(t)
    while queue:
        nid = queue.pop(0)
        order.append(nid)
        for t in sorted(downstream[nid]):
            indeg[t] -= 1
            if indeg[t] == 0:
                queue.append(t)
    if len(order) < len(nodes):
        raise WorkflowError("流程中存在环，请检查连线")

    sources: list[dict] = []
    steps: list[dict] = []
    # 允许多个 output：每个 output 独立编译一条终点 SQL；output_sql 取第一个作主输出预览
    # （通知/xlsx/运行结果面板等只需要一个主输出；其他 output 作为副产出在画布上可各自预览）。
    output_sql = None
    last_view = None

    for nid in order:
        n = nodes[nid]
        typ, name, cfg = n.get("type"), n["name"].strip(), n.get("cfg") or {}
        if typ == "source":
            conn = (cfg.get("conn") or "").strip()
            if "/" not in conn:
                raise WorkflowError(f"取数节点「{name}」未选择连接")
            if not (cfg.get("sql") or "").strip():
                raise WorkflowError(f"取数节点「{name}」缺少 SQL")
            project, connection = conn.split("/", 1)
            sources.append({"kind": "connection", "node": nid, "dataset": name,
                            "project": project, "connection": connection,
                            "sql": cfg["sql"].strip(),
                            "limit": cfg.get("limit"), "schema": cfg.get("schema") or None})
        elif typ == "file":
            if not (cfg.get("path") or "").strip():
                raise WorkflowError(f"文件节点「{name}」缺少路径")
            sources.append({"kind": "file", "node": nid, "dataset": name,
                            "path": cfg["path"].strip()})
        elif typ == "filter":
            up = _one_input(n)
            where = (cfg.get("where") or "").strip()
            if not where:
                raise WorkflowError(f"过滤节点「{name}」缺少 WHERE 条件")
            sql = f"SELECT * FROM {_q(up['name'])} WHERE {where}"
            steps.append({"node": nid, "name": name,
                          "sql": f"CREATE OR REPLACE VIEW {_q(name)} AS {sql}"})
            last_view = name
        elif typ == "join":
            # 端口：新格式 in_1..in_N；老图 left→in_1、right→in_2 兼容层
            ports = dict(inputs[nid])
            if "left" in ports and "in_1" not in ports:
                ports["in_1"] = ports.pop("left")
            if "right" in ports and "in_2" not in ports:
                ports["in_2"] = ports.pop("right")
            ordered: list[tuple[int, str]] = []
            for k, v in ports.items():
                if not k.startswith("in_"):
                    continue
                try:
                    idx = int(k.split("_", 1)[1])
                except ValueError:
                    continue
                ordered.append((idx, v))
            ordered.sort(key=lambda x: x[0])
            if len(ordered) < 2:
                raise WorkflowError(f"JOIN 节点「{name}」至少需要两个输入")
            if len(ordered) > 16:
                raise WorkflowError(f"JOIN 节点「{name}」最多支持 16 路输入")
            aliases = "abcdefghijklmnop"
            on = (cfg.get("on") or "").strip()
            if not on:
                raise WorkflowError(
                    f"JOIN 节点「{name}」缺少 ON 条件（用 a/b/c... 引用各输入表；两路时 a/b 等价老 l/r）")
            kind = (cfg.get("kind") or "INNER").upper()
            if kind not in ("INNER", "LEFT", "RIGHT", "FULL"):
                raise WorkflowError(f"JOIN 类型 {kind!r} 不支持")
            n = len(ordered)
            default_cols = ", ".join(f"{aliases[i]}.*" for i in range(n))
            # 两路兼容：用户老配置里 SELECT 可能写 "l.*, r.*"，替换成 a.*/b.*
            raw_cols = (cfg.get("select") or default_cols).strip()
            if n == 2 and ("l." in raw_cols or "r." in raw_cols):
                raw_cols = raw_cols.replace("l.", "a.").replace("r.", "b.")
            cols = raw_cols
            # 两路兼容：ON 里的 l./r. 也翻译成 a./b.
            if n == 2 and ("l." in on or "r." in on):
                on = on.replace("l.", "a.").replace("r.", "b.")
            # 组装 SQL。SQL 里每个 JOIN 必须有自己的 ON 子句（DuckDB/标准 SQL）；
            # 用户只填一段整体 ON，方案：中间的 N-2 个 JOIN 都填 ON TRUE 占位，
            # 用户整段 ON 放最后一个 JOIN 后。语义等价、DuckDB 优化器会合并处理。
            first_id = ordered[0][1]
            sql = f"SELECT {cols} FROM {_q(nodes[first_id]['name'])} {aliases[0]}"
            tail = ordered[1:]
            for i, (_idx, from_id) in enumerate(tail, start=1):
                sql += f" {kind} JOIN {_q(nodes[from_id]['name'])} {aliases[i]}"
                # 中间 JOIN 用 ON TRUE 占位；最后一个 JOIN 接用户整段条件
                if i < len(tail):
                    sql += " ON TRUE"
                else:
                    sql += f" ON ({on})"
            steps.append({"node": nid, "name": name,
                          "sql": f"CREATE OR REPLACE VIEW {_q(name)} AS {sql}"})
            last_view = name
        elif typ == "aggregate":
            up = _one_input(n)
            aggs = (cfg.get("aggs") or "").strip()
            if not aggs:
                raise WorkflowError(f"聚合节点「{name}」缺少聚合表达式（如 count(*) AS n）")
            group = (cfg.get("group") or "").strip()
            select = f"{group}, {aggs}" if group else aggs
            sql = f"SELECT {select} FROM {_q(up['name'])}"
            if group:
                sql += f" GROUP BY {group}"
            steps.append({"node": nid, "name": name,
                          "sql": f"CREATE OR REPLACE VIEW {_q(name)} AS {sql}"})
            last_view = name
        elif typ == "sql":
            raw = (cfg.get("sql") or "").strip().rstrip(";")
            if not raw:
                raise WorkflowError(f"SQL 节点「{name}」内容为空")
            steps.append({"node": nid, "name": name,
                          "sql": f"CREATE OR REPLACE VIEW {_q(name)} AS ({raw})"})
            last_view = name
        elif typ == "output":
            up = _one_input(n)
            sql = f"SELECT * FROM {_q(up['name'])}"
            if (cfg.get("order_by") or "").strip():
                sql += f" ORDER BY {cfg['order_by'].strip()}"
            limit = cfg.get("limit")
            sql += f" LIMIT {int(limit)}" if limit else " LIMIT 1000"
            # 第一个 output 作为主输出预览（多 output 时，其余在 steps 里也会执行）
            if output_sql is None:
                output_sql = sql
            steps.append({"node": nid, "name": name, "sql": sql, "is_output": True})
        else:
            raise WorkflowError(f"未知节点类型 {typ!r}")

    if output_sql is None:  # 没画输出节点：预览最后一个视图（若全是数据源则预览最后的源）
        tail = last_view or (sources[-1]["dataset"] if sources else None)
        if tail:
            output_sql = f"SELECT * FROM {_q(tail)} LIMIT 1000"
            steps.append({"node": None, "name": "预览", "sql": output_sql})
    return {"sources": sources, "steps": steps, "output_sql": output_sql}


def split_statements(script: str) -> list[str]:
    """按分号切分多条语句（跳过引号与注释），供 workflow 逐条执行。"""
    out, start, i, n = [], 0, 0, len(script)
    while i < n:
        c = script[i]
        two = script[i:i + 2]
        if c in ("'", '"'):
            q = c
            i += 1
            while i < n:
                if script[i] == "\\":
                    i += 2
                    continue
                if script[i] == q:
                    if q == "'" and script[i + 1:i + 2] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
        elif two == "--":
            while i < n and script[i] != "\n":
                i += 1
        elif two == "/*":
            i += 2
            while i < n and script[i:i + 2] != "*/":
                i += 1
            i += 2
        elif c == ";":
            stmt = script[start:i].strip()
            if stmt:
                out.append(stmt)
            i += 1
            start = i
        else:
            i += 1
    tail = script[start:].strip()
    if tail:
        out.append(tail)
    return out


# =========================================================
# 调度：cron 匹配纯函数 + 下拉转 cron + 两张 SQLite 表
# =========================================================
# 5 字段 cron：分 时 日 月 周（0=周日；1-7 也认 7=周日）。支持 *  N  N,M  N-M  */N。
# 意图：避免引入 croniter 依赖，纯函数 40 行足够；tick 粒度 30s 即可。

_CRON_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
# 周字段允许 7 = 周日（cron 惯例），展开时映射为 0
_CRON_DOW_MAX_INPUT = 7


def _cron_field(field: str, lo: int, hi: int, allow_max: int | None = None) -> set[int]:
    """把单个字段展开成允许值集合；非法返回 None（调用方判无效）。"""
    if not field:
        return set()
    vals: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            return set()
        step = 1
        if "/" in part:
            base, step_s = part.split("/", 1)
            try:
                step = int(step_s)
            except ValueError:
                return set()
            if step < 1:
                return set()
        else:
            base = part
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a, b = base.split("-", 1)
            try:
                start, end = int(a), int(b)
            except ValueError:
                return set()
            if start > end:
                return set()
        else:
            try:
                v = int(base)
            except ValueError:
                return set()
            start = end = v
        upper = allow_max if allow_max is not None else hi
        if start < lo or end > upper:
            return set()
        vals.update(range(start, end + 1, step))
    return vals


def cron_matches(cron: str, dt) -> bool:  # noqa: ANN001
    """判断 datetime dt 是否命中 cron 表达式。5 字段：分 时 日 月 周（0=周日=7）。

    dt 建议是"本地时间"（scheduler tick 用 datetime.now()）。cron 语义与常见 Unix
    cron 一致：日和周任一命中即算匹配（除非其中一个是 *，此时只看另一个）。
    """
    parts = (cron or "").strip().split()
    if len(parts) != 5:
        return False
    fields = []
    for i, p in enumerate(parts):
        lo, hi = _CRON_RANGES[i]
        # 周字段允许输入 7（=周日），后续映射到 0
        allow_max = _CRON_DOW_MAX_INPUT if i == 4 else None
        s = _cron_field(p, lo, hi, allow_max=allow_max)
        # 周字段 7 视为 0（周日）
        if i == 4 and 7 in s:
            s = (s - {7}) | {0}
        if not s:
            return False
        fields.append(s)
    minute, hour, day, month, dow = fields
    dow_v = dt.weekday()  # Python: 周一=0 … 周日=6
    dow_v = (dow_v + 1) % 7  # 换算成 cron 惯例：周日=0 … 周六=6
    # 分/时/月直接比对
    if dt.minute not in minute or dt.hour not in hour or dt.month not in month:
        return False
    # 日/周：任一 * 时只看另一个；都不是 * 时任一命中即可（Unix cron 语义）
    day_star = parts[2] == "*"
    dow_star = parts[4] == "*"
    day_hit = dt.day in day
    dow_hit = dow_v in dow
    if day_star and dow_star:
        return True
    if day_star:
        return dow_hit
    if dow_star:
        return day_hit
    return day_hit or dow_hit


def cron_from_dropdown(cron_type: str, cron_value: str) -> str:
    """把简易下拉转成 cron 表达式。cron_type/cron_value 见 workflow_schedule 表 doc。

    - interval:N            → "*/N * * * *"（N ≤ 59）或 "0 */H * * *"（N=60H）
    - daily:HH:MM           → "MM HH * * *"
    - weekly:W HH:MM        → "MM HH * * W"（W=0..6，0=周日）
    - monthly:D HH:MM       → "MM HH D * *"
    - cron:表达式           → 原样

    非法输入抛 ValueError。
    """
    t, v = (cron_type or "").strip(), (cron_value or "").strip()
    if t == "cron":
        if len((v or "").split()) != 5:
            raise ValueError(f"cron 表达式须为 5 字段：{v!r}")
        return v
    if t == "interval":
        try:
            n = int(v)
        except ValueError as e:
            raise ValueError(f"interval 需要整数分钟：{v!r}") from e
        if n < 1:
            raise ValueError(f"interval 最少 1 分钟：{n}")
        if n <= 59:
            return f"*/{n} * * * *"
        if n % 60 == 0 and n // 60 < 24:
            return f"0 */{n // 60} * * *"
        raise ValueError(f"interval 只支持 1-59 或 60 的倍数（<24h）：{n}")
    if t == "daily":
        h, m = _parse_hhmm(v)
        return f"{m} {h} * * *"
    if t == "weekly":
        parts = v.split(None, 1)
        if len(parts) != 2:
            raise ValueError(f"weekly 格式：'W HH:MM'（W=0..6）：{v!r}")
        try:
            w = int(parts[0])
        except ValueError as e:
            raise ValueError(f"weekly 星期需为整数 0..6：{parts[0]!r}") from e
        if not 0 <= w <= 6:
            raise ValueError(f"weekly 星期须 0..6：{w}")
        h, m = _parse_hhmm(parts[1])
        return f"{m} {h} * * {w}"
    if t == "monthly":
        parts = v.split(None, 1)
        if len(parts) != 2:
            raise ValueError(f"monthly 格式：'D HH:MM'（D=1..31）：{v!r}")
        try:
            d = int(parts[0])
        except ValueError as e:
            raise ValueError(f"monthly 日期需为整数 1..31：{parts[0]!r}") from e
        if not 1 <= d <= 31:
            raise ValueError(f"monthly 日期须 1..31：{d}")
        h, m = _parse_hhmm(parts[1])
        return f"{m} {h} {d} * *"
    raise ValueError(f"未知调度类型：{t!r}")


def _parse_hhmm(s: str) -> tuple[int, int]:
    if not s or ":" not in s:
        raise ValueError(f"HH:MM 格式非法：{s!r}")
    h_s, m_s = s.strip().split(":", 1)
    try:
        h, m = int(h_s), int(m_s)
    except ValueError as e:
        raise ValueError(f"HH:MM 需为整数：{s!r}") from e
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"HH:MM 越界：{s!r}")
    return h, m


_SCHEDULE_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_schedule (
    name             TEXT PRIMARY KEY,
    cron_type        TEXT NOT NULL,
    cron_value       TEXT NOT NULL,
    enabled          INTEGER NOT NULL DEFAULT 1,
    notify_on        TEXT NOT NULL DEFAULT 'failure',
    attach_kinds     TEXT NOT NULL DEFAULT '["summary"]',
    notify_channels  TEXT NOT NULL DEFAULT '',
    last_run_at      TEXT,
    last_status      TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
"""

_RUN_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_run (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    status         TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    triggered_by   TEXT NOT NULL,
    steps_json     TEXT NOT NULL DEFAULT '[]',
    output_preview TEXT NOT NULL DEFAULT '',
    error          TEXT NOT NULL DEFAULT '',
    xlsx_path      TEXT
);
CREATE INDEX IF NOT EXISTS idx_workflow_run_name_started ON workflow_run (name, started_at DESC);
"""


class WorkflowScheduleStore:
    """调度配置存储（每 workflow 至多一条）。默认注入前禁用（daemon-only）。"""

    def __init__(self, db_path: str | Path):
        db_path = Path(db_path)
        if str(db_path) != ":memory:":
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEDULE_SCHEMA)
            self._conn.commit()

    def upsert(self, name: str, cron_type: str, cron_value: str, enabled: bool = True,
               notify_on: str = "failure", attach_kinds: list[str] | None = None,
               notify_channels: list[str] | None = None) -> dict:
        # 参数校验：抛 ValueError 由上层转 400
        cron_from_dropdown(cron_type, cron_value)
        if notify_on not in ("success", "failure", "always", "none"):
            raise ValueError(f"notify_on 须为 success/failure/always/none：{notify_on!r}")
        kinds = attach_kinds or ["summary"]
        for k in kinds:
            if k not in ("summary", "markdown_table", "xlsx_link"):
                raise ValueError(f"attach_kinds 未知：{k!r}")
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute(
                "INSERT INTO workflow_schedule"
                " (name, cron_type, cron_value, enabled, notify_on, attach_kinds,"
                "  notify_channels, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(name) DO UPDATE SET cron_type=excluded.cron_type,"
                " cron_value=excluded.cron_value, enabled=excluded.enabled,"
                " notify_on=excluded.notify_on, attach_kinds=excluded.attach_kinds,"
                " notify_channels=excluded.notify_channels, updated_at=excluded.updated_at",
                (name, cron_type, cron_value, 1 if enabled else 0, notify_on,
                 json.dumps(kinds, ensure_ascii=False),
                 json.dumps(notify_channels or [], ensure_ascii=False), now, now))
            self._conn.commit()
        return self.get(name)

    def get(self, name: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM workflow_schedule WHERE name = ?", (name,)).fetchone()
        return _schedule_row(row) if row else None

    def list(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM workflow_schedule ORDER BY name").fetchall()
        return [_schedule_row(r) for r in rows]

    def list_enabled(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM workflow_schedule WHERE enabled = 1").fetchall()
        return [_schedule_row(r) for r in rows]

    def delete(self, name: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM workflow_schedule WHERE name = ?", (name,))
            self._conn.commit()

    def mark_ran(self, name: str, status: str) -> None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute(
                "UPDATE workflow_schedule SET last_run_at = ?, last_status = ? WHERE name = ?",
                (now, status, name))
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _schedule_row(row: sqlite3.Row) -> dict:
    return {"name": row["name"], "cron_type": row["cron_type"], "cron_value": row["cron_value"],
            "enabled": bool(row["enabled"]), "notify_on": row["notify_on"],
            "attach_kinds": json.loads(row["attach_kinds"] or "[]"),
            "notify_channels": json.loads(row["notify_channels"] or "[]"),
            "last_run_at": row["last_run_at"], "last_status": row["last_status"],
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


class WorkflowRunStore:
    """workflow 运行历史（含 steps/output_preview/xlsx_path，30 天保留）。"""

    def __init__(self, db_path: str | Path):
        db_path = Path(db_path)
        if str(db_path) != ":memory:":
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_RUN_SCHEMA)
            self._conn.commit()

    def start(self, name: str, triggered_by: str = "manual") -> int:
        """新建一条 running 记录，返回 id。"""
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO workflow_run (name, status, started_at, triggered_by)"
                " VALUES (?, 'running', ?, ?)", (name, now, triggered_by))
            self._conn.commit()
            return cur.lastrowid

    def finish(self, run_id: int, status: str, steps: list[dict] | None = None,
               output_preview: dict | None = None, error: str = "",
               xlsx_path: str | None = None) -> None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute(
                "UPDATE workflow_run SET status = ?, finished_at = ?, steps_json = ?,"
                " output_preview = ?, error = ?, xlsx_path = ? WHERE id = ?",
                (status, now, json.dumps(steps or [], ensure_ascii=False),
                 json.dumps(output_preview or {}, ensure_ascii=False),
                 error, xlsx_path, run_id))
            self._conn.commit()

    def get(self, run_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM workflow_run WHERE id = ?", (run_id,)).fetchone()
        return _run_row(row) if row else None

    def list_by_name(self, name: str, limit: int = 50) -> list[dict]:
        with self._lock:
            # 用 id 作 tie-breaker（同秒新建的记录 started_at 相同）
            rows = self._conn.execute(
                "SELECT * FROM workflow_run WHERE name = ?"
                " ORDER BY started_at DESC, id DESC LIMIT ?",
                (name, limit)).fetchall()
        return [_run_row(r) for r in rows]

    def running_for(self, name: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM workflow_run WHERE name = ? AND status = 'running'",
                (name,)).fetchall()
        return [_run_row(r) for r in rows]

    def sweep_stale_running(self, older_than_hours: int = 1) -> int:
        """服务重启清理：把很久前还标 running 的记录标 failed，避免永久阻塞下次调度。"""
        cutoff = datetime.now(UTC).timestamp() - older_than_hours * 3600
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, started_at FROM workflow_run WHERE status = 'running'").fetchall()
            stale = []
            for r in rows:
                try:
                    ts = datetime.fromisoformat(r["started_at"]).timestamp()
                except ValueError:
                    continue
                if ts < cutoff:
                    stale.append(r["id"])
            if stale:
                now = datetime.now(UTC).isoformat(timespec="seconds")
                self._conn.executemany(
                    "UPDATE workflow_run SET status = 'failed', finished_at = ?,"
                    " error = '服务重启中断' WHERE id = ?",
                    [(now, sid) for sid in stale])
                self._conn.commit()
        return len(stale)

    def purge_older_than(self, days: int) -> list[str]:
        """删除超过 N 天的运行记录，返回被清的 xlsx_path 列表供上层 rm 目录。"""
        cutoff = (datetime.now(UTC).timestamp() - days * 86400)
        with self._lock:
            rows = self._conn.execute(
                "SELECT xlsx_path FROM workflow_run WHERE started_at < ?",
                (datetime.fromtimestamp(cutoff, UTC).isoformat(timespec="seconds"),)).fetchall()
            paths = [r["xlsx_path"] for r in rows if r["xlsx_path"]]
            self._conn.execute(
                "DELETE FROM workflow_run WHERE started_at < ?",
                (datetime.fromtimestamp(cutoff, UTC).isoformat(timespec="seconds"),))
            self._conn.commit()
        return paths

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _run_row(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "name": row["name"], "status": row["status"],
            "started_at": row["started_at"], "finished_at": row["finished_at"],
            "triggered_by": row["triggered_by"],
            "steps": json.loads(row["steps_json"] or "[]"),
            "output_preview": json.loads(row["output_preview"] or "{}"),
            "error": row["error"], "xlsx_path": row["xlsx_path"]}
