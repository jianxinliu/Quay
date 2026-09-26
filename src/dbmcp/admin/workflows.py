"""流程（workflow / DAG）页与接口。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import anyio.to_thread
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from .context import AdminContext


def mount(ctx: AdminContext) -> None:
    mcp = ctx.mcp
    service = ctx.service
    guard = ctx.guard
    _shell = ctx._shell
    _caller = ctx._caller
    _resolve_conn = ctx._resolve_conn
    _jobmgr = ctx._jobmgr
    _solo_key = ctx._solo_key

    @mcp.custom_route("/admin/workflows/list", methods=["GET"])
    @guard
    async def _wf_list(_req: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "workflows": service.workflow_list()})

    @mcp.custom_route("/admin/workflows", methods=["GET"])
    @guard
    async def _wf_page(_req: Request) -> HTMLResponse:
        """流程独立页——Vue 挂载点，SPA 内部路由（列表页 / 详情页）由 workflows.js 判断。"""
        body = ('<div id="wf-app"></div>'
                '<link rel="stylesheet" href="/admin/static/workflows.css">'
                # echarts UMD 必须先于任何 AMD loader 加载才会挂 window.echarts
                # （见 CLAUDE.md 里 UMD/AMD 坑记录）；本页无 monaco loader，纯预防
                '<script src="/admin/static/echarts.min.js"></script>'
                '<script src="/admin/static/vue.global.prod.js"></script>'
                '<script src="/admin/static/dg-select.js"></script>'
                '<script src="/admin/static/workflows.js"></script>')
        return _shell("流程", body, doc=False)

    @mcp.custom_route("/admin/workflows/save", methods=["POST"])
    @guard
    async def _wf_save(req: Request) -> JSONResponse:
        import json as _json

        f = await req.form()
        chart_raw = str(f.get("chart") or "")
        graph_raw = str(f.get("graph") or "")
        try:
            chart = _json.loads(chart_raw) if chart_raw else None
            if chart is not None and not isinstance(chart, dict):
                chart = None
            graph = _json.loads(graph_raw) if graph_raw else None
            if graph is not None and not isinstance(graph, dict):
                graph = None
            wf = await anyio.to_thread.run_sync(
                service.workflow_save, str(f.get("name") or ""),
                str(f.get("workspace") or ""), str(f.get("script") or ""), _caller(req),
                chart, graph)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "workflow": wf})

    @mcp.custom_route("/admin/workflows/delete", methods=["POST"])
    @guard
    async def _wf_delete(req: Request) -> JSONResponse:
        f = await req.form()
        try:
            await anyio.to_thread.run_sync(service.workflow_delete, str(f.get("name") or ""))
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True})

    @mcp.custom_route("/admin/workflows/ai", methods=["POST"])
    @guard
    async def _wf_ai(req: Request) -> JSONResponse:
        """让 AI 按连接/表结构 + 需求生成/修改 workflow DAG（compile 校验+重修）。回前端载到画布，不执行。

        current_graph 传当前流程 JSON 时走修改模式（AI 在此基础上按需求增/删/改）；否则新建。
        """
        from ..service import QueryRejected
        if not service.get_settings().get("ai_enabled"):
            return JSONResponse({"ok": False, "error": "AI 辅助未开启"}, status_code=403)
        f = await req.form()
        question = str(f.get("question") or "")
        schema = str(f.get("schema") or "").strip() or None
        try:
            tables = json.loads(str(f.get("tables") or "[]"))
            tables = [str(t).strip() for t in tables if str(t).strip()] or None
        except (ValueError, TypeError):
            tables = None
        current_graph = None
        cg_raw = str(f.get("current_graph") or "").strip()
        if cg_raw:
            try:
                cg = json.loads(cg_raw)
                if isinstance(cg, dict) and (cg.get("nodes") or []):
                    current_graph = cg
            except (ValueError, TypeError):
                pass
        caller = _caller(req)
        try:
            project, connection = _resolve_conn(str(f.get("conn") or ""))
            out = await anyio.to_thread.run_sync(
                lambda: service.ai_generate_workflow(
                    project, connection, question, caller,
                    schema=schema, tables=tables, current_graph=current_graph))
        except (QueryRejected, KeyError, ValueError) as e:
            return JSONResponse({"ok": False, "error": str(e)})
        return JSONResponse({"ok": True, **out})

    @mcp.custom_route("/admin/workflows/run", methods=["POST"])
    @guard
    async def _wf_run(req: Request) -> JSONResponse:
        f = await req.form()
        name = str(f.get("name") or "")
        caller = _caller(req)

        def _work_wf(_register, _report=None) -> dict:  # noqa: ANN001
            return {"kind": "workflow", **service.workflow_run(name, caller)}

        job_id = _jobmgr.submit(_solo_key(), _work_wf)
        return JSONResponse({"ok": True, "job_id": job_id})

    @mcp.custom_route("/admin/workflows/run_graph", methods=["POST"])
    @guard
    async def _wf_run_graph(req: Request) -> JSONResponse:
        """直接运行画布 DAG（无需先保存）。异步 job，结果 kind=workflow（steps 带 node id）。"""
        import json as _json
        f = await req.form()
        workspace = str(f.get("workspace") or "")
        try:
            graph = _json.loads(str(f.get("graph") or ""))
        except ValueError:
            return JSONResponse({"ok": False, "error": "graph 不是合法 JSON"}, status_code=400)
        caller = _caller(req)

        def _work_graph(_register, _report=None) -> dict:  # noqa: ANN001
            return {"kind": "workflow", **service.workflow_run_graph(workspace, graph, caller)}

        job_id = _jobmgr.submit(_solo_key(), _work_graph)
        return JSONResponse({"ok": True, "job_id": job_id})

    @mcp.custom_route("/admin/workflows/preview_columns", methods=["POST"])
    @guard
    async def _wf_preview_columns(req: Request) -> JSONResponse:
        """拿目标节点输出的列 schema（编译到该节点前依赖，DESCRIBE 该 view）。

        Body: graph=<json>, workspace, node, refresh?=0/1。
        返回 {ok, columns:[{name,type}]} 或 {ok, columns:[], error:"..."}。
        """
        import json as _json
        f = await req.form()
        workspace = str(f.get("workspace") or "").strip()
        node_id = str(f.get("node") or "").strip()
        refresh = str(f.get("refresh") or "").strip() in ("1", "true", "yes")
        if not workspace or not node_id:
            return JSONResponse({"ok": False, "error": "workspace 与 node 必填"}, status_code=400)
        try:
            graph = _json.loads(str(f.get("graph") or ""))
            if not isinstance(graph, dict):
                raise ValueError("graph 必须是对象")
        except ValueError as e:
            return JSONResponse({"ok": False, "error": f"graph JSON 非法：{e}"}, status_code=400)
        try:
            out = await anyio.to_thread.run_sync(
                lambda: service.workflow_preview_columns(workspace, graph, node_id,
                                                        _caller(req), refresh))
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, **out})

    @mcp.custom_route("/admin/workflows/preview_node", methods=["POST"])
    @guard
    async def _wf_preview_node(req: Request) -> JSONResponse:
        """预览目标节点输出的前 N 行（模块视图「查看输出」）。"""
        import json as _json
        f = await req.form()
        workspace = str(f.get("workspace") or "").strip()
        node_id = str(f.get("node") or "").strip()
        try:
            limit = int(str(f.get("limit") or "100"))
        except ValueError:
            limit = 100
        if not workspace or not node_id:
            return JSONResponse({"ok": False, "error": "workspace 与 node 必填"}, status_code=400)
        try:
            graph = _json.loads(str(f.get("graph") or ""))
            if not isinstance(graph, dict):
                raise ValueError("graph 必须是对象")
        except ValueError as e:
            return JSONResponse({"ok": False, "error": f"graph JSON 非法：{e}"}, status_code=400)
        try:
            out = await anyio.to_thread.run_sync(
                lambda: service.workflow_preview_node(workspace, graph, node_id, _caller(req),
                                                     limit))
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, **out})

    @mcp.custom_route("/admin/workflows/workspaces", methods=["GET"])
    @guard
    async def _wf_workspaces(_req: Request) -> JSONResponse:
        """列已有工作区（新页新建 workflow 时下拉用）。"""
        try:
            out = await anyio.to_thread.run_sync(service.analysis_overview)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "workspaces": out})

    @mcp.custom_route("/admin/workflows/workspace_create", methods=["POST"])
    @guard
    async def _wf_workspace_create(req: Request) -> JSONResponse:
        """新建分析工作区（新页新建 workflow 时的「＋ 新建工作区…」入口）。"""
        f = await req.form()
        name = str(f.get("name") or "").strip()
        if not name:
            return JSONResponse({"ok": False, "error": "工作区名不能为空"}, status_code=400)
        try:
            store = service._require_analysis()  # noqa: SLF001
            await anyio.to_thread.run_sync(store.create_workspace, name)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "workspace": name})

    # ---------- 调度（PR-2） ----------

    @mcp.custom_route("/admin/workflows/schedule", methods=["GET"])
    @guard
    async def _wf_schedule_get(req: Request) -> JSONResponse:
        """取指定 workflow 的调度配置：?name=xxx"""
        name = req.query_params.get("name") or ""
        if not name:
            return JSONResponse({"ok": False, "error": "name 必填"}, status_code=400)
        try:
            sched = await anyio.to_thread.run_sync(service.workflow_schedule_get, name)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "schedule": sched})

    @mcp.custom_route("/admin/workflows/schedule", methods=["POST"])
    @guard
    async def _wf_schedule_upsert(req: Request) -> JSONResponse:
        """增/改调度：name / cron_type / cron_value / enabled(0/1) / notify_on /
        attach_kinds(逗号分隔或 JSON)。"""
        import json as _json
        f = await req.form()
        name = str(f.get("name") or "").strip()
        cron_type = str(f.get("cron_type") or "").strip()
        cron_value = str(f.get("cron_value") or "").strip()
        enabled = str(f.get("enabled") or "1").strip() not in ("0", "false", "no", "")
        notify_on = str(f.get("notify_on") or "failure").strip()
        raw_kinds = str(f.get("attach_kinds") or "").strip()
        kinds: list[str] | None = None
        if raw_kinds:
            try:
                parsed = _json.loads(raw_kinds)
                if isinstance(parsed, list):
                    kinds = [str(x) for x in parsed]
            except ValueError:
                kinds = [x.strip() for x in raw_kinds.split(",") if x.strip()]
        if not name or not cron_type or not cron_value:
            return JSONResponse(
                {"ok": False, "error": "name / cron_type / cron_value 必填"},
                status_code=400)
        try:
            sched = await anyio.to_thread.run_sync(
                lambda: service.workflow_schedule_upsert(
                    name, cron_type, cron_value,
                    enabled=enabled, notify_on=notify_on, attach_kinds=kinds))
        except (ValueError, RuntimeError) as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "schedule": sched})

    @mcp.custom_route("/admin/workflows/schedule/delete", methods=["POST"])
    @guard
    async def _wf_schedule_delete(req: Request) -> JSONResponse:
        f = await req.form()
        name = str(f.get("name") or "").strip()
        if not name:
            return JSONResponse({"ok": False, "error": "name 必填"}, status_code=400)
        try:
            await anyio.to_thread.run_sync(service.workflow_schedule_delete, name)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True})

    # ---------- 定时任务全局管理 ----------

    @mcp.custom_route("/admin/workflows/schedules", methods=["GET"])
    @guard
    async def _wf_schedules_list(req: Request) -> JSONResponse:
        """所有定时任务列表（附 workflow_exists / running 标记）。定时任务管理页用。"""
        try:
            rows = await anyio.to_thread.run_sync(service.workflow_schedules_enriched)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "schedules": rows})

    @mcp.custom_route("/admin/workflows/schedule/trigger", methods=["POST"])
    @guard
    async def _wf_schedule_trigger(req: Request) -> JSONResponse:
        """立即触发一次调度（后台线程跑，走跟 cron 到点相同的链路）。"""
        form = await req.form()
        name = (form.get("name") or "").strip()
        if not name:
            return JSONResponse({"ok": False, "error": "name 必填"}, status_code=400)
        try:
            await anyio.to_thread.run_sync(service.workflow_schedule_trigger_now, name)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True})

    # ---------- 运行历史 & 详情 & xlsx 下载 ----------

    @mcp.custom_route("/admin/workflows/running", methods=["GET"])
    @guard
    async def _wf_running(req: Request) -> JSONResponse:
        """当前调度触发正在执行的 workflow 列表。前端 5s 轮询。

        默认只列 triggered_by=schedule（手动触发的用户在前端等结果、不进管理面板）；
        ?triggered_by=all 可看全部。"""
        raw = (req.query_params.get("triggered_by") or "schedule").strip()
        filt: str | None = None if raw == "all" else raw
        try:
            runs = await anyio.to_thread.run_sync(
                lambda: service.workflow_running_list(filt))
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "runs": runs})

    @mcp.custom_route("/admin/workflows/runs", methods=["GET"])
    @guard
    async def _wf_runs(req: Request) -> JSONResponse:
        """?name=xxx&limit=50 → 该 workflow 的运行历史（started_at DESC）。"""
        name = req.query_params.get("name") or ""
        try:
            limit = int(req.query_params.get("limit") or "50")
        except ValueError:
            limit = 50
        if not name:
            return JSONResponse({"ok": False, "error": "name 必填"}, status_code=400)
        try:
            runs = await anyio.to_thread.run_sync(
                lambda: service.workflow_runs_list(name, limit))
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "runs": runs})

    @mcp.custom_route("/admin/workflows/runs/{run_id:int}/detail", methods=["GET"])
    @guard
    async def _wf_run_detail(req: Request) -> JSONResponse:
        try:
            run_id = int(req.path_params["run_id"])
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "run_id 非法"}, status_code=400)
        try:
            run = await anyio.to_thread.run_sync(service.workflow_run_get, run_id)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        if run is None:
            return JSONResponse({"ok": False, "error": "运行记录不存在"}, status_code=404)
        return JSONResponse({"ok": True, "run": run})

    @mcp.custom_route("/admin/workflows/runs/{run_id:int}", methods=["GET"])
    @guard
    async def _wf_run_page(req: Request) -> HTMLResponse:
        """运行详情页 HTML shell（Vue 挂载点由 workflows.js 里的 App 处理）。"""
        body = ('<div id="wf-app"></div>'
                '<link rel="stylesheet" href="/admin/static/workflows.css">'
                '<script src="/admin/static/echarts.min.js"></script>'
                '<script src="/admin/static/vue.global.prod.js"></script>'
                '<script src="/admin/static/dg-select.js"></script>'
                '<script src="/admin/static/workflows.js"></script>')
        return _shell("运行详情", body, doc=False)

    @mcp.custom_route(
        "/admin/workflows/runs/{run_id:int}/download/output.xlsx", methods=["GET"])
    @guard
    async def _wf_run_xlsx(req: Request) -> Response:
        """下载 workflow 运行的 xlsx 产物（仅当 attach_kinds 含 xlsx_link 时存在）。"""
        try:
            run_id = int(req.path_params["run_id"])
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "run_id 非法"}, status_code=400)
        run = service.workflow_run_get(run_id)
        if not run or not run.get("xlsx_path"):
            return JSONResponse({"ok": False, "error": "xlsx 不存在"}, status_code=404)
        if not service.data_dir:
            return JSONResponse({"ok": False, "error": "data_dir 未配置"}, status_code=500)
        full = Path(service.data_dir) / run["xlsx_path"]
        try:
            data = full.read_bytes()
        except OSError:
            return JSONResponse({"ok": False, "error": "xlsx 文件缺失"}, status_code=404)
        return Response(
            content=data,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition":
                     f'attachment; filename="workflow-run-{run_id}.xlsx"'})
