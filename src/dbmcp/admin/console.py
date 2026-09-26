"""查询台 /admin/sql/* 接口：树、执行、异步任务、导出、片段、体检。"""

from __future__ import annotations

import json
from functools import partial
from typing import TYPE_CHECKING

import anyio.to_thread
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from .common import _engine_dialect, _format_sql, error_payload
from .context import AdminContext


# 查询台页面：Vue 3 + Monaco 深色 IDE。页面只给挂载点与脚本，逻辑在 static/console.js，
# 数据全走 /admin/sql/* JSON 接口（连接/表/结构/执行/导出/片段），与服务端渲染解耦。
def _lint_one(sql: str, dialect: str) -> list[dict]:
    """对单块 SQL 做 sqlglot 语法检查，返回错误列表（行列相对本块文本）。"""
    import re as _re

    import sqlglot
    from sqlglot.errors import ParseError, SqlglotError

    from ..audit.classify import normalize_sql_for_parse

    if not sql.strip():
        return []
    try:
        # DB 合法但 sqlglot 解析不了的语法（如 MySQL 无括号 DROP PARTITION）先归一化，
        # 避免编辑器对合法语句误标红波浪线（dialect 名与 engine 名一致）。
        sqlglot.parse(normalize_sql_for_parse(sql, dialect), read=dialect)
        return []
    except ParseError as e:
        out = []
        for err in getattr(e, "errors", [])[:5]:
            out.append({"line": err.get("line"), "col": err.get("col"),
                        "message": err.get("description") or str(e)})
        return out or [{"line": 1, "col": 1, "message": str(e)}]
    except SqlglotError as e:  # 词法错误等（版本间类名有变：TokenizeError/TokenError）→ 基类兜底
        m = _re.search(r"Line (\d+), Col: (\d+)", str(e))
        return [{"line": int(m.group(1)) if m else 1, "col": int(m.group(2)) if m else 1,
                 "message": str(e).split("\n")[0][:200]}]
    except Exception:  # noqa: BLE001  lint 永不抛错影响编辑
        return []


def _split_blank_line_blocks(sql: str) -> list[tuple[int, str]]:
    """按**空行**把 SQL 拆成块（连续非空行为一块），返回 [(起始行号 1-indexed, 文本)]。
    与前端 stmtRanges 的「空行也分隔语句」一致。"""
    blocks: list[tuple[int, str]] = []
    cur: list[str] = []
    start = 0
    for i, ln in enumerate(sql.split("\n"), 1):
        if ln.strip() == "":
            if cur:
                blocks.append((start, "\n".join(cur)))
                cur = []
        else:
            if not cur:
                start = i
            cur.append(ln)
    if cur:
        blocks.append((start, "\n".join(cur)))
    return blocks


def _lint_sql(sql: str, dialect: str) -> list[dict]:
    """sqlglot 语法检查（查询台编辑器标红）。只报错误，不阻断任何执行路径——
    执行时的「默认拒绝」判定在 classify/assess，与这里无关。

    关键：sqlglot 只按分号拆多语句、不认空行分隔。所以「无分号 + 空行分隔的多条」会被当成
    一条、在下一条起始处误报语法错（红波浪线标到错的语句上，用户反馈的坑）。修法：整体能解析
    就直接放行（避免拆块假阳性，如字符串内的空行）；整体报错时才按空行拆块逐块 lint，把错误
    行号回填到原文——这样每一块独立解析，合法块不报错、错误也落在真正出错的那条上。"""
    if not sql.strip():
        return []
    errs = _lint_one(sql, dialect)
    if not errs:
        return []
    blocks = _split_blank_line_blocks(sql)
    if len(blocks) <= 1:
        return errs   # 只有一块 → 就是这块的错，行号已正确
    out: list[dict] = []
    for start_line, text in blocks:
        for e in _lint_one(text, dialect):
            e = dict(e)
            if e.get("line"):
                e["line"] = e["line"] + start_line - 1   # 块内行号 → 原文行号（列不变，块从行首起）
            out.append(e)
        if len(out) >= 5:
            break
    return out[:5]


def _console_body() -> str:
    return (
        '<link rel="stylesheet" href="/admin/static/console.css">'
        '<div id="dbm-console"></div>'
        '<script src="/admin/static/vue.global.prod.js"></script>'
        # echarts 必须先于 Monaco 的 AMD loader：loader.js 定义 define.amd 后，
        # echarts 的 UMD 会走 AMD 注册而不挂 window.echarts
        '<script src="/admin/static/echarts.min.js"></script>'
        '<script src="/admin/static/monaco/vs/loader.js"></script>'
        # MySQL 内置函数文档（编辑器 hover 用），须先于 console.js 加载
        '<script src="/admin/static/sqlfuncs.js"></script>'
        # 共享自绘下拉（console/redis/workflows 三页共用），须先于 console.js
        '<script src="/admin/static/dg-select.js"></script>'
        # 「用户与权限」弹窗组件（左树右键菜单打开），同样须先于 console.js
        '<link rel="stylesheet" href="/admin/static/privileges.css">'
        '<script src="/admin/static/privileges.js"></script>'
        '<script src="/admin/static/console.js"></script>'
    )


def mount(ctx: AdminContext) -> None:
    mcp = ctx.mcp
    service = ctx.service
    guard = ctx.guard
    _shell = ctx._shell
    _caller = ctx._caller
    _analysis_ws = ctx._analysis_ws
    _resolve_conn = ctx._resolve_conn
    _jobmgr = ctx._jobmgr
    _solo_key = ctx._solo_key

    @mcp.custom_route("/admin/sql", methods=["GET"])
    @guard
    async def _sql_console(_req: Request) -> HTMLResponse:
        return _shell("查询台", _console_body(), doc=False)

    @mcp.custom_route("/admin/sql/connections", methods=["GET"])
    @guard
    async def _sql_connections(_req: Request) -> JSONResponse:
        conns = []
        for pname, proj in sorted(service.config.projects.items()):
            for cname, c in sorted(proj.connections.items()):
                conns.append({
                    "value": f"{pname}/{cname}", "project": pname, "connection": cname,
                    "engine": c.engine, "environment": c.environment or "",
                    "database": c.database or "",
                })
        workspaces = []
        if service.analysis is not None:
            try:
                workspaces = [w["workspace"] for w in service.analysis.list_workspaces()]
            except Exception:
                workspaces = []
        return JSONResponse({"ok": True, "connections": conns, "workspaces": workspaces,
                             "ai_enabled": bool(service.get_settings().get("ai_enabled"))})

    @mcp.custom_route("/admin/sql/databases", methods=["GET"])
    @guard
    async def _sql_databases(req: Request) -> JSONResponse:
        """列 schema（MySQL 下即库）。PG 带 db= 时列的是该 database 里的 schema。"""
        db = req.query_params.get("db") or None
        try:
            project, connection = _resolve_conn(req.query_params.get("conn", ""))
            dbs = await anyio.to_thread.run_sync(
                service.list_databases, project, connection, _caller(req), db)
        except Exception as e:
            return JSONResponse(error_payload(e))
        return JSONResponse({"ok": True, "databases": dbs})

    @mcp.custom_route("/admin/sql/server_databases", methods=["GET"])
    @guard
    async def _sql_server_databases(req: Request) -> JSONResponse:
        """列服务器上的 database。PG 才有意义（库与 schema 是两层）；其它引擎等价于上面那个。"""
        try:
            project, connection = _resolve_conn(req.query_params.get("conn", ""))
            dbs = await anyio.to_thread.run_sync(
                service.list_server_databases, project, connection, _caller(req))
        except Exception as e:
            return JSONResponse(error_payload(e))
        return JSONResponse({"ok": True, "databases": dbs})

    @mcp.custom_route("/admin/sql/tables", methods=["GET"])
    @guard
    async def _sql_tables(req: Request) -> JSONResponse:
        schema = req.query_params.get("schema") or None
        ws = _analysis_ws(req.query_params.get("conn", ""))
        if ws:
            try:
                datasets = await anyio.to_thread.run_sync(service.analysis.list_datasets, ws)
            except Exception as e:
                return JSONResponse({"ok": False, "error": str(e)})
            return JSONResponse({"ok": True,
                                 "tables": [d["name"] for d in datasets],
                                 "sizes": {}})
        try:
            project, connection = _resolve_conn(req.query_params.get("conn", ""))
            tables = await anyio.to_thread.run_sync(
                service.list_tables, project, connection, _caller(req), schema,
                req.query_params.get("db") or None)
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)})
        try:  # 表容量（树右侧分级展示）：拿不到不阻断表列表
            sizes = await anyio.to_thread.run_sync(
                service.admin_table_sizes, project, connection, _caller(req), schema,
                req.query_params.get("db") or None)
        except Exception:
            sizes = {}
        return JSONResponse({"ok": True, "tables": tables, "sizes": sizes})

    @mcp.custom_route("/admin/sql/table", methods=["GET"])
    @guard
    async def _sql_table(req: Request) -> JSONResponse:
        schema = req.query_params.get("schema") or None
        ws = _analysis_ws(req.query_params.get("conn", ""))
        if ws:
            try:
                info = await anyio.to_thread.run_sync(
                    service.analysis.describe_dataset, ws, req.query_params.get("table", ""))
            except Exception as e:
                return JSONResponse({"ok": False, "error": str(e)})
            return JSONResponse({"ok": True, **info})
        try:
            project, connection = _resolve_conn(req.query_params.get("conn", ""))
            table = req.query_params.get("table", "")
            info = await anyio.to_thread.run_sync(
                service.describe_table, project, connection, table, _caller(req), schema,
                req.query_params.get("db") or None)
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)})
        return JSONResponse({"ok": True, **info})

    @mcp.custom_route("/admin/sql/search_tables", methods=["GET"])
    @guard
    async def _sql_search_tables(req: Request) -> JSONResponse:
        conn = req.query_params.get("conn") or ""
        q = req.query_params.get("q") or ""
        if "/" not in conn or not q.strip():
            return JSONResponse({"ok": True, "results": []})
        project, connection = conn.split("/", 1)
        try:
            out = await anyio.to_thread.run_sync(
                lambda: service.admin_search_tables(
                    project, connection, q, _caller(req),
                    req.query_params.get("db") or None))
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "results": out})

    @mcp.custom_route("/admin/sql/lint", methods=["POST"])
    @guard
    async def _sql_lint(req: Request) -> JSONResponse:
        """编辑器实时语法检查：sqlglot 按方言 parse，返回首个错误的行列。"""
        f = await req.form()
        sql = str(f.get("sql") or "")
        engine = str(f.get("dialect") or "mysql")
        # 方言来自驱动注册表（分析工作台的 duckdb 也在其中）；未注册的引擎退回 mysql
        dialect = _engine_dialect(engine) or "mysql"
        return JSONResponse({"ok": True, "errors": _lint_sql(sql, dialect)})

    @mcp.custom_route("/admin/sql/import", methods=["POST"])
    @guard
    async def _sql_import(req: Request) -> JSONResponse:
        """查询台数据导入：前端解析 CSV/粘贴为 rows JSON，此处参数化批量 INSERT。"""
        import json as _json
        f = await req.form()
        conn = str(f.get("conn") or "")
        if "/" not in conn:
            return JSONResponse({"ok": False, "error": "缺少连接"}, status_code=400)
        project, connection = conn.split("/", 1)
        try:
            columns = _json.loads(str(f.get("columns") or "[]"))
            rows = _json.loads(str(f.get("rows") or "[]"))
        except ValueError:
            return JSONResponse({"ok": False, "error": "columns/rows 不是合法 JSON"}, status_code=400)
        try:
            out = await anyio.to_thread.run_sync(
                lambda: service.admin_import_rows(
                    project, connection, str(f.get("table") or ""), columns, rows,
                    _caller(req), schema=str(f.get("schema") or "") or None,
                    database=str(f.get("db") or "") or None))
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, **out})

    @mcp.custom_route("/admin/analysis/import", methods=["POST"])
    @guard
    async def _analysis_import(req: Request) -> JSONResponse:
        from ..service import QueryRejected
        f = await req.form()
        try:
            project, connection = _resolve_conn(str(f.get("conn") or ""))
            limit_raw = str(f.get("limit") or "").strip()
            out = await anyio.to_thread.run_sync(
                service.analysis_import,
                str(f.get("workspace") or ""), str(f.get("dataset") or ""),
                project, connection, str(f.get("sql") or ""), _caller(req),
                int(limit_raw) if limit_raw else None,
                str(f.get("schema") or "").strip() or None)
        except (QueryRejected, KeyError, ValueError) as e:
            return JSONResponse({"ok": False, "error": str(e)})
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"})
        return JSONResponse({"ok": True, **out})

    @mcp.custom_route("/admin/sql/history", methods=["GET"])
    @guard
    async def _sql_history(req: Request) -> JSONResponse:
        try:
            ws = _analysis_ws(req.query_params.get("conn", ""))
            if ws:
                items = await anyio.to_thread.run_sync(
                    service.admin_query_history, "analysis", ws)
                return JSONResponse({"ok": True, "items": items})
            project, connection = _resolve_conn(req.query_params.get("conn", ""))
            items = await anyio.to_thread.run_sync(
                service.admin_query_history, project, connection)
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)})
        return JSONResponse({"ok": True, "items": items})

    @mcp.custom_route("/admin/sql/explain", methods=["POST"])
    @guard
    async def _sql_explain(req: Request) -> JSONResponse:
        from ..service import QueryRejected
        f = await req.form()
        schema = str(f.get("schema") or "").strip() or None
        db = str(f.get("db") or "").strip() or None   # PG：执行所在的 database
        try:
            project, connection = _resolve_conn(str(f.get("conn") or ""))
            out = await anyio.to_thread.run_sync(
                service.admin_explain, project, connection, str(f.get("sql") or ""),
                _caller(req), schema, db)
        except (QueryRejected, KeyError, ValueError) as e:
            return JSONResponse({"ok": False, "error": str(e)})
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"})
        return JSONResponse({"ok": True, **out})

    @mcp.custom_route("/admin/sql/ddl", methods=["GET"])
    @guard
    async def _sql_ddl(req: Request) -> JSONResponse:
        schema = req.query_params.get("schema") or None
        ws = _analysis_ws(req.query_params.get("conn", ""))
        if ws:
            try:
                ddl = await anyio.to_thread.run_sync(
                    service.analysis.get_ddl, ws, req.query_params.get("table", ""))
            except Exception as e:
                return JSONResponse({"ok": False, "error": str(e)})
            return JSONResponse({"ok": True, "ddl": ddl})
        try:
            project, connection = _resolve_conn(req.query_params.get("conn", ""))
            table = req.query_params.get("table", "")
            ddl = await anyio.to_thread.run_sync(
                service.get_table_ddl, project, connection, table, _caller(req), schema,
                req.query_params.get("db") or None)
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)})
        return JSONResponse({"ok": True, "ddl": ddl})

    @mcp.custom_route("/admin/sql/ai", methods=["POST"])
    @guard
    async def _sql_ai(req: Request) -> JSONResponse:
        """让命令行 AI 按表结构 + 需求生成一条 SQL。只返回文本、前端回填编辑器，不执行。"""
        from ..service import QueryRejected
        if not service.get_settings().get("ai_enabled"):
            return JSONResponse({"ok": False, "error": "AI 辅助未开启"}, status_code=403)
        f = await req.form()
        question = str(f.get("question") or "")
        schema = str(f.get("schema") or "").strip() or None
        explain = str(f.get("explain") or "") in ("1", "on", "true")
        samples = str(f.get("include_samples") or "") in ("1", "on", "true")
        session_id = str(f.get("session_id") or "").strip() or None
        db = str(f.get("db") or "").strip() or None   # PG：取表结构所在的 database
        try:
            tables = json.loads(str(f.get("tables") or "[]"))
            tables = [str(t).strip() for t in tables if str(t).strip()] or None
        except (ValueError, TypeError):
            tables = None
        caller = _caller(req)
        try:
            project, connection = _resolve_conn(str(f.get("conn") or ""))
            out = await anyio.to_thread.run_sync(
                lambda: service.ai_generate_sql(
                    project, connection, question, caller, schema=schema, tables=tables,
                    explain=explain, include_samples=samples, session_id=session_id,
                    database=db))
        except (QueryRejected, KeyError, ValueError) as e:
            return JSONResponse({"ok": False, "error": str(e)})
        # 美化 SQL 再回给前端（AI 常吐一长条）；解析失败则原样返回
        engine = service.config.get_connection(project, connection).engine
        out["sql"] = _format_sql(out.get("sql") or "", engine)
        return JSONResponse({"ok": True, **out})

    @mcp.custom_route("/admin/sql/run_async", methods=["POST"])
    @guard
    async def _sql_run_async(req: Request) -> JSONResponse:
        f = await req.form()
        sql = str(f.get("sql") or "")
        confirm = str(f.get("confirm") or "") in ("1", "on", "true")
        confirm_text = str(f.get("confirm_text") or "") or None
        expect_fp = str(f.get("expect_fingerprint") or "") or None
        try:
            page = max(int(str(f.get("page") or "0")), 0)
        except ValueError:
            page = 0
        schema = str(f.get("schema") or "").strip() or None
        db = str(f.get("db") or "").strip() or None   # PG：执行所在的 database
        caller = _caller(req)
        from ..jobs import Busy
        ws = _analysis_ws(str(f.get("conn") or ""))
        if ws:
            # 分析工作区：沙箱内任意 SQL 自由执行（不需确认流）。按工作区串行——忙时直接拒绝；
            # 不透传取消器（DuckDB 沙箱查询本地、无 KILL 路径）。
            def _work_ws(_register, _report=None) -> dict:  # noqa: ANN001
                out = service.analysis_sql(ws, sql, caller)
                return {"kind": "read", "paginated": False, **out}

            try:
                job_id = _jobmgr.submit(("analysis", ws), _work_ws)
            except Busy:
                return JSONResponse({"ok": False,
                                     "error": f"工作区 {ws} 有查询正在执行，请等待其完成后再试。"})
            return JSONResponse({"ok": True, "job_id": job_id})
        try:
            project, connection = _resolve_conn(str(f.get("conn") or ""))
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)})

        def _work(register, report=None) -> dict:  # noqa: ANN001
            try:
                return service.admin_run_sql(project, connection, sql, caller,
                                             confirm, page, None, schema, on_start=register,
                                             confirm_text=confirm_text, expect_fingerprint=expect_fp,
                                             database=db)
            except Exception as e:  # noqa: BLE001
                # 统一成已脱敏的文案；连接类错误打上 dbm_error_kind，JobManager 会把它
                # 透传到 /admin/sql/job 快照里，前端据此在结果区直接显示「重连」按钮。
                payload = error_payload(e)
                wrapped = RuntimeError(payload["error"])
                wrapped.dbm_error_kind = payload.get("error_kind", "")
                raise wrapped from e

        # 按连接串行只约束编辑器里手写的 SQL（同一连接同时只跑一条，忙时直接拒绝、前端明确提示）；
        # 双击表名打开的数据 tab（parallel=1）用独立 key 立即并行、不占用连接串行名额、也不互拒。
        # 取消器经 on_start 注册，运行中取消会对 DB 发 KILL QUERY / pg_cancel。
        parallel = str(f.get("parallel") or "") in ("1", "on", "true")
        queue_key = _solo_key() if parallel else (project, connection)
        try:
            job_id = _jobmgr.submit(queue_key, _work)
        except Busy:
            return JSONResponse({"ok": False,
                                 "error": f"连接 {project}/{connection} 有查询正在执行，"
                                          "请等待其完成，或点击「取消」中断后再试。"})
        return JSONResponse({"ok": True, "job_id": job_id})

    @mcp.custom_route("/admin/sql/job", methods=["GET"])
    @guard
    async def _sql_job(req: Request) -> JSONResponse:
        job_id = req.query_params.get("id", "")
        snap = _jobmgr.get(job_id)
        if snap is None:
            return JSONResponse({"ok": False, "error": "任务不存在或已过期（结果保留 10 分钟）"})
        out = {"ok": True, "status": snap["status"], "elapsed_ms": snap["elapsed_ms"]}
        # 运行中的长任务（异步导出）带实时进度：{rows: 已导出行数}
        if snap["status"] == "running" and snap.get("progress") is not None:
            out["progress"] = snap["progress"]
        if snap["status"] == "done":
            out["result"] = snap["result"]
        elif snap["status"] in ("error", "canceled"):
            out["error"] = snap["error"]
            out["error_kind"] = snap.get("error_kind") or ""
        return JSONResponse(out)

    @mcp.custom_route("/admin/sql/cancel", methods=["POST"])
    @guard
    async def _sql_cancel(req: Request) -> JSONResponse:
        """取消正在执行的任务：对 DB 发 KILL QUERY / pg_cancel_backend / interrupt。"""
        f = await req.form()
        job_id = str(f.get("id") or "")
        return JSONResponse({"ok": _jobmgr.cancel(job_id)})

    @mcp.custom_route("/admin/sql/health", methods=["GET"])
    @guard
    async def _sql_health(_req: Request) -> JSONResponse:
        """各连接的健康位快照，供查询台左树/连接选择器画状态灯。

        只返回**非健康**的连接（健康的不占带宽，前端把「查不到」当作正常）。
        retry_in_s 是距离下一次自动重试的秒数——前端据此显示「N 秒后自动重连」，
        让人知道系统在自愈、不必手忙脚乱地点重连。
        """
        import time as _time

        snap = service.health.snapshot()
        now = _time.monotonic()
        conns = {
            f"{proj}/{conn}": {
                "state": h.state,
                "fail_count": h.fail_count,
                "retry_in_s": max(0, int(h.next_retry_at - now)),
                "probing": h.probing,
                "last_error": h.last_error,
            }
            for (proj, conn), h in snap.items() if h.state != "ok"
        }
        return JSONResponse({"ok": True, "conns": conns})

    @mcp.custom_route("/admin/sql/checkup", methods=["GET"])
    @guard
    async def _sql_checkup(req: Request) -> JSONResponse:
        """数据库体检：一次返回结构化诊断报告（连接/缓存/长查询/锁/复制/大表…）。

        和 agent 的 db_checkup 工具同一套逻辑（service.db_checkup），只是走后台鉴权，
        供查询台连接栏的「体检」按钮调用。schema 为 MySQL/ClickHouse 的库、PG 的 schema。
        """
        try:
            project, connection = _resolve_conn(req.query_params.get("conn", ""))
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        schema = req.query_params.get("schema", "").strip() or None
        database = req.query_params.get("db", "").strip() or None  # 仅 PG：在哪个 database 上体检
        # all=1：实例级体检——未选具体库时前端传 all，诊断覆盖该连接下的全体库
        # （慢查询/大表在哪个库都可能发生），而不是只看当前库。单库引擎自动回落。
        instance_level = req.query_params.get("all", "").strip() in ("1", "true", "yes")
        try:
            if instance_level:
                report = await anyio.to_thread.run_sync(
                    partial(service.db_checkup_all, project, connection, _caller(req),
                            database=database))
            else:
                report = await anyio.to_thread.run_sync(
                    partial(service.db_checkup, project, connection, _caller(req),
                            schema=schema, database=database))
        except Exception as e:  # noqa: BLE001
            return JSONResponse(error_payload(e), status_code=400)
        return JSONResponse({"ok": True, "report": report})

    @mcp.custom_route("/admin/sql/checkup/ai", methods=["POST"])
    @guard
    async def _sql_checkup_ai(req: Request) -> JSONResponse:
        """让 AI 根据体检报告给出诊断建议（纯文本分析，不执行任何 SQL）。

        前端把体检浮层里的报告 JSON 回传；服务端补上连接侧非敏感信息后喂给 AI。
        也可在没报告时直接调（服务端现场重跑体检）。追问带 session_id 续接会话。
        """
        from ..service import QueryRejected

        if not service.get_settings().get("ai_enabled"):
            return JSONResponse({"ok": False, "error": "AI 辅助未开启"}, status_code=403)
        f = await req.form()
        try:
            project, connection = _resolve_conn(str(f.get("conn") or ""))
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        question = str(f.get("question") or "")
        schema = str(f.get("schema") or "").strip() or None
        database = str(f.get("db") or "").strip() or None
        session_id = str(f.get("session_id") or "").strip() or None
        # 连接级体检（未选库）的诊断针对全体库；服务端缺报告时按这个标志重跑体检
        all_dbs = str(f.get("all") or "").strip() in ("1", "true", "yes")
        report = None
        raw = str(f.get("report") or "").strip()
        if raw:
            try:
                obj = json.loads(raw)
                report = obj if isinstance(obj, dict) else None
            except (ValueError, TypeError):
                return JSONResponse({"ok": False, "error": "体检报告 JSON 解析失败"}, status_code=400)
        try:
            out = await anyio.to_thread.run_sync(
                lambda: service.ai_diagnose_checkup(
                    project, connection, question, _caller(req),
                    report=report, schema=schema, database=database, session_id=session_id,
                    all_dbs=all_dbs))
        except (QueryRejected, KeyError, ValueError) as e:
            return JSONResponse({"ok": False, "error": str(e)})
        except Exception as e:  # noqa: BLE001
            return JSONResponse(error_payload(e), status_code=400)
        return JSONResponse({"ok": True, **out})

    @mcp.custom_route("/admin/sql/reconnect", methods=["POST"])
    @guard
    async def _sql_reconnect(req: Request) -> JSONResponse:
        """人工重连：连接被判 unavailable/exhausted 后，从查询台点「重连」强制重建。

        绕过健康位（否则 exhausted 连接连测试都做不了），成功即恢复可用。
        """
        f = await req.form()
        raw = str(f.get("conn") or "")
        if _analysis_ws(raw):
            return JSONResponse({"ok": False, "error": "分析工作区为本地沙箱，无需重连"})
        try:
            project, connection = _resolve_conn(raw)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)})
        try:
            out = await anyio.to_thread.run_sync(
                lambda: service.reconnect_connection(project, connection, _caller(req)))
        except Exception as e:  # noqa: BLE001
            return JSONResponse(error_payload(e))
        return JSONResponse(out)

    @mcp.custom_route("/admin/sql/assess_batch", methods=["POST"])
    @guard
    async def _sql_assess_batch(req: Request) -> JSONResponse:
        f = await req.form()
        try:
            stmts = json.loads(str(f.get("stmts") or "[]"))
            if not isinstance(stmts, list) or not all(isinstance(x, str) for x in stmts):
                raise ValueError("stmts 须为字符串数组")
        except (ValueError, json.JSONDecodeError) as e:
            return JSONResponse({"ok": False, "error": f"参数错误：{e}"})
        # 分析工作区是本地沙箱，语句本来就不需要确认
        if _analysis_ws(str(f.get("conn") or "")):
            return JSONResponse({"ok": True, "kind": "read", "count": len(stmts)})
        schema = str(f.get("schema") or "").strip() or None
        db = str(f.get("db") or "").strip() or None
        try:
            project, connection = _resolve_conn(str(f.get("conn") or ""))
            out = await anyio.to_thread.run_sync(
                lambda: service.admin_assess_batch(project, connection, stmts, _caller(req),
                                                   schema=schema, database=db))
        except Exception as e:  # noqa: BLE001
            return JSONResponse(error_payload(e))
        return JSONResponse({"ok": True, **out})

    @mcp.custom_route("/admin/sql/run", methods=["POST"])
    @guard
    async def _sql_run(req: Request) -> JSONResponse:
        f = await req.form()
        sql = str(f.get("sql") or "")
        confirm = str(f.get("confirm") or "") in ("1", "on", "true")
        confirm_text = str(f.get("confirm_text") or "") or None
        expect_fp = str(f.get("expect_fingerprint") or "") or None
        try:
            page = max(int(str(f.get("page") or "0")), 0)
        except ValueError:
            page = 0
        schema = str(f.get("schema") or "").strip() or None
        db = str(f.get("db") or "").strip() or None   # PG：执行所在的 database
        ws = _analysis_ws(str(f.get("conn") or ""))
        if ws:
            try:
                out = await anyio.to_thread.run_sync(
                    service.analysis_sql, ws, sql, _caller(req))
            except Exception as e:  # noqa: BLE001
                return JSONResponse({"ok": False, "error": str(e)})
            # 沙箱 DDL/DML 无结果集时按 write 形态返回（批量 DROP 等复用前端逻辑）
            if not out["columns"]:
                return JSONResponse({"ok": True, "kind": "write", "affected_rows": 0,
                                     "duration_ms": 0})
            return JSONResponse({"ok": True, "kind": "read", "paginated": False, **out})
        try:
            project, connection = _resolve_conn(str(f.get("conn") or ""))
            result = await anyio.to_thread.run_sync(
                lambda: service.admin_run_sql(
                    project, connection, sql, _caller(req), confirm, page, None, schema,
                    confirm_text=confirm_text, expect_fingerprint=expect_fp, database=db))
        except Exception as e:  # noqa: BLE001
            return JSONResponse(error_payload(e))
        return JSONResponse({"ok": True, **result})

    @mcp.custom_route("/admin/sql/format", methods=["POST"])
    @guard
    async def _sql_format(req: Request) -> JSONResponse:
        f = await req.form()
        sql = str(f.get("sql") or "")
        engine = ""
        try:
            project, connection = _resolve_conn(str(f.get("conn") or ""))
            engine = service.config.get_connection(project, connection).engine
        except Exception:
            pass
        return JSONResponse({"ok": True, "sql": _format_sql(sql, engine)})

    def _export_filename(project: str, connection: str, ext: str) -> str:
        import re
        from datetime import datetime
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        slug = re.sub(r"[^A-Za-z0-9_.-]", "-", f"{project}-{connection}")
        return f"{slug}-{stamp}.{ext}"

    @mcp.custom_route("/admin/sql/export", methods=["POST"])
    @guard
    async def _sql_export(req: Request) -> Response:
        """同步导出（小结果）：直接返回文件字节。大结果请走 /admin/sql/export_async。"""
        from ..export import ExportError, export_result
        from ..service import QueryRejected
        f = await req.form()
        sql = str(f.get("sql") or "")
        fmt = str(f.get("format") or "csv")
        schema = str(f.get("schema") or "").strip() or None
        db = str(f.get("db") or "").strip() or None   # PG：执行所在的 database
        ws = _analysis_ws(str(f.get("conn") or ""))
        try:
            if ws:
                out = await anyio.to_thread.run_sync(
                    service.analysis_sql, ws, sql, _caller(req), 100_000)
                data = export_result(out["columns"], out["rows"], fmt)
                media_type, ext = data[1], data[2]
                project, connection = "analysis", ws
            else:
                project, connection = _resolve_conn(str(f.get("conn") or ""))
                res = await anyio.to_thread.run_sync(
                    service.admin_export, project, connection, sql, fmt, _caller(req),
                    schema, db)
                data, media_type, ext = res["data"], res["media_type"], res["ext"]
        except (QueryRejected, KeyError, ValueError, ExportError) as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=400)
        return Response(data, media_type=media_type,
                        headers={"Content-Disposition":
                                 f'attachment; filename="{_export_filename(project, connection, ext)}"'})

    # 异步导出：完整结果集可能上百万行、跑几十秒，不能让一次 HTTP 请求扛着——
    # 提交进任务队列在后台跑，前端弹框看进度、可关掉（后台继续）、完成了再提醒下载。
    # 文件落服务端（复用 agent 导出的 token 目录），返回带 token 的下载链接，
    # 而不是把几个 MB 的 blob 塞进 job 快照。
    @mcp.custom_route("/admin/sql/export_async", methods=["POST"])
    @guard
    async def _sql_export_async(req: Request) -> JSONResponse:
        from ..export import SUPPORTED_FORMATS
        from ..jobs import Busy
        f = await req.form()
        sql = str(f.get("sql") or "")
        fmt = str(f.get("format") or "csv")
        schema = str(f.get("schema") or "").strip() or None
        db = str(f.get("db") or "").strip() or None   # PG：执行所在的 database
        caller = _caller(req)
        ws = _analysis_ws(str(f.get("conn") or ""))

        # 只读校验在提交前同步做：写 SQL 立即给 400，比丢进任务再失败体验好
        if fmt not in SUPPORTED_FORMATS:
            return JSONResponse(
                {"ok": False, "error": f"不支持的导出格式 {fmt!r}，可选：{', '.join(SUPPORTED_FORMATS)}"},
                status_code=400)

        if ws:
            # 分析工作区：沙箱 DuckDB，不受 classify 约束；行数上限给大一些
            def _work(_register, report):  # noqa: ANN001
                from ..export import export_result
                out = service.analysis_sql(ws, sql, caller, 100_000)
                if report:
                    report({"rows": out["row_count"], "stage": "serializing"})
                data, media_type, ext = export_result(out["columns"], out["rows"], fmt)
                summary = {
                    "project": "analysis", "connection": ws,
                    "database": ws, "table": "query",
                    "row_count": out["row_count"], "truncated": out.get("truncated", False),
                    "format": fmt, "columns": out["columns"],
                }
                return {**service._save_mcp_export(data, media_type, ext, summary),
                        "duration_ms": out.get("duration_ms", 0)}
        else:
            try:
                project, connection = _resolve_conn(str(f.get("conn") or ""))
            except Exception as e:
                return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
            # 提交前先判定只读（同步返回错误，不占任务槽）
            from ..audit.classify import classify
            engine_kind = service.config.get_connection(project, connection).engine
            if not classify(sql, engine_kind).readonly:
                return JSONResponse(
                    {"ok": False, "error": "导出仅支持只读查询（SELECT/SHOW/...）的结果"},
                    status_code=400)

            def _work(register, report):  # noqa: ANN001
                try:
                    res = service.admin_export(
                        project, connection, sql, fmt, caller, schema, db,
                        on_progress=lambda n: report({"rows": n}) if report else None,
                        on_start=register)
                except Exception as e:  # noqa: BLE001
                    payload = error_payload(e)
                    wrapped = RuntimeError(payload["error"])
                    wrapped.dbm_error_kind = payload.get("error_kind", "")
                    raise wrapped from e
                data, media_type, ext = res["data"], res["media_type"], res["ext"]
                summary = {
                    "project": project, "connection": connection,
                    "database": db or "", "table": "query",
                    "row_count": res["row_count"], "truncated": res["truncated"],
                    "format": fmt, "columns": res["columns"],
                }
                saved = service._save_mcp_export(data, media_type, ext, summary)
                # 完成提醒：写收件箱（SSE 推给还开着的页面 → 铃铛 +1；
                # 页面关了也没关系，下次打开看未读数）。用户要的「导出成功再提醒」。
                try:
                    fname = saved.get("filename") or "export"
                    service.notifier.send(
                        title="导出完成",
                        body=f"{fname}（{res['row_count']} 行，"
                             f"{res['duration_ms'] / 1000:.1f}s）",
                        meta={"kind": "export",
                              "deeplink": saved.get("download_url") or ""})
                except Exception:  # noqa: BLE001 — 通知失败不影响导出结果
                    pass
                return {**saved, "duration_ms": res["duration_ms"]}

        try:
            job_id = _jobmgr.submit(_solo_key(), _work)   # 独立 key：不占用连接串行名额
        except Busy:
            return JSONResponse(
                {"ok": False, "error": "已有导出任务正在执行，请等待其完成或取消后再试。"})
        return JSONResponse({"ok": True, "job_id": job_id})

    # ---------- SQL 片段库 ----------

    @mcp.custom_route("/admin/sql/snippets", methods=["GET"])
    @guard
    async def _snippets_list(_req: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "snippets": service.list_snippets()})

    @mcp.custom_route("/admin/sql/snippets/save", methods=["POST"])
    @guard
    async def _snippets_save(req: Request) -> JSONResponse:
        from ..snippets import SnippetError
        f = await req.form()
        sid_raw = str(f.get("id") or "").strip()
        try:
            snippet = service.save_snippet(
                title=str(f.get("title") or ""),
                sql=str(f.get("sql") or ""),
                note=str(f.get("note") or ""),
                connection=str(f.get("connection") or ""),
                snippet_id=int(sid_raw) if sid_raw else None,
            )
        except (SnippetError, ValueError) as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "snippet": snippet})

    @mcp.custom_route("/admin/sql/snippets/delete", methods=["POST"])
    @guard
    async def _snippets_delete(req: Request) -> JSONResponse:
        from ..snippets import SnippetError
        f = await req.form()
        try:
            service.delete_snippet(int(str(f.get("id") or "0")))
        except (SnippetError, ValueError) as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True})
