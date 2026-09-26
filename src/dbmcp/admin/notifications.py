"""站内通知：未读数、列表、已读、测试、SSE 流。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio.to_thread
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

from .context import AdminContext


def mount(ctx: AdminContext) -> None:
    mcp = ctx.mcp
    service = ctx.service
    guard = ctx.guard

    # ---------- 通知（站内铃铛 + SSE + 测试发送）----------

    def _get_with_timeout(q, timeout: float):  # noqa: ANN001
        """SSE 用：把 queue.get(timeout) 交给线程池（run_sync 只接受位置参数）。"""
        return q.get(True, timeout)

    @mcp.custom_route("/admin/notifications/unread_count", methods=["GET"])
    @guard
    async def _notify_unread(_req: Request) -> JSONResponse:
        if service.inbox is None:
            return JSONResponse({"ok": True, "count": 0})
        return JSONResponse({"ok": True, "count": service.inbox.unread_count()})

    @mcp.custom_route("/admin/notifications/list", methods=["GET"])
    @guard
    async def _notify_list(req: Request) -> JSONResponse:
        if service.inbox is None:
            return JSONResponse({"ok": True, "items": []})
        try:
            limit = min(int(req.query_params.get("limit") or 20), 200)
        except ValueError:
            limit = 20
        unread_only = req.query_params.get("unread") in ("1", "true", "yes")
        items = [n.to_dict() for n in service.inbox.list_recent(limit=limit, unread_only=unread_only)]
        return JSONResponse({"ok": True, "items": items})

    @mcp.custom_route("/admin/notifications/mark_read", methods=["POST"])
    @guard
    async def _notify_mark_read(req: Request) -> JSONResponse:
        if service.inbox is None:
            return JSONResponse({"ok": True, "updated": 0})
        f = await req.form()
        ids_raw = str(f.get("ids") or "").strip()
        all_flag = str(f.get("all") or "") in ("1", "true", "yes", "on")
        if all_flag:
            n = service.inbox.mark_all_read()
        else:
            ids: list[int] = []
            for part in ids_raw.split(","):
                part = part.strip()
                if not part:
                    continue
                try:
                    ids.append(int(part))
                except ValueError:
                    continue
            n = service.inbox.mark_read(ids)
        return JSONResponse({"ok": True, "updated": n})

    @mcp.custom_route("/admin/notifications/test", methods=["POST"])
    @guard
    async def _notify_test(_req: Request) -> JSONResponse:
        try:
            service.notifier.send(
                title="Quay 测试通知",
                body="如果你收到了，说明当前配置的通知渠道工作正常。",
                meta={"kind": "test"},
            )
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)})
        return JSONResponse({"ok": True})

    @mcp.custom_route("/admin/notifications/stream", methods=["GET"])
    @guard
    async def _notify_stream(req: Request) -> StreamingResponse:
        """SSE 流：每条新通知实时推给已登录的后台页面。

        用 anyio.to_thread 把阻塞的 queue.get 挪到线程池，避免霸占 event loop。
        客户端断连时 anyio 抛 CancelledError，我们清订阅退出。
        """
        if service.inbox is None:
            async def _empty():
                yield b": inbox disabled\n\n"
            return StreamingResponse(_empty(), media_type="text/event-stream")

        inbox = service.inbox
        q = inbox.subscribe()

        async def _gen():
            import json as _json  # noqa: PLC0415
            import queue as _q  # noqa: PLC0415
            try:
                # 打招呼：让客户端立即知道连接成功
                yield b": ok\n\n"
                while True:
                    if await req.is_disconnected():
                        return
                    # 等下一条（最多 25s），到点发心跳；断连由 is_disconnected 兜底
                    try:
                        n = await anyio.to_thread.run_sync(_get_with_timeout, q, 25)
                    except _q.Empty:
                        yield b": ping\n\n"
                        continue
                    if n is None:  # close sentinel
                        return
                    payload = _json.dumps(n.to_dict(), ensure_ascii=False)
                    yield f"event: notification\ndata: {payload}\n\n".encode("utf-8")
            finally:
                inbox.unsubscribe(q)

        return StreamingResponse(_gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})
