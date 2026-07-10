"""入口：dbm 命令。

子命令：
- serve（默认）：daemon（streamable HTTP）或 --stdio
- approvals：列出审批单（默认 pending）
- approve <id> / reject <id>：CLI 审批兜底，直接读写审批 SQLite

保持向后兼容：`dbm --port 8100` 等旧用法等价于 `dbm serve --port 8100`。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .approvals import ApprovalError, ApprovalStore
from .audit.log import AuditStore
from .config import load_config
from .metadata import MetadataCache
from .server import build_mcp
from .service import DbmService

DEFAULT_CONFIG = os.environ.get("DBM_CONFIG", "config/connections.yaml")
DEFAULT_DATA_DIR = os.environ.get("DBM_DATA_DIR", "data")
DEFAULT_HOST = os.environ.get("DBM_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("DBM_PORT", "8100"))

_SUBCOMMANDS = {"serve", "approvals", "approve", "reject"}


def _add_data_dir(p: argparse.ArgumentParser) -> None:
    p.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="SQLite 数据目录")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dbm", description="db-manage-mcp 服务与审批 CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="运行 MCP 服务（默认子命令）")
    serve.add_argument("--config", default=DEFAULT_CONFIG, help="连接配置 YAML 路径")
    _add_data_dir(serve)
    serve.add_argument("--stdio", action="store_true", help="以 stdio 传输运行（默认 HTTP daemon）")
    serve.add_argument("--host", default=DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)

    approvals = sub.add_parser("approvals", help="列出审批单")
    _add_data_dir(approvals)
    approvals.add_argument("--status", default="pending", help="pending/approved/rejected/consumed，all 为全部")

    for name, help_text in (("approve", "批准审批单"), ("reject", "拒绝审批单")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("change_id", type=int)
        _add_data_dir(p)
        p.add_argument("--by", default=os.environ.get("USER", "cli"), help="审批人（默认当前系统用户）")
        p.add_argument("--note", default="", help="备注/拒绝理由（会返回给 agent）")

    return parser


def _open_approvals(data_dir: str) -> ApprovalStore:
    db = Path(data_dir) / "dbm.sqlite3"
    if not db.exists():
        sys.exit(f"数据文件不存在: {db}（daemon 还没运行过？用 --data-dir 指定目录）")
    return ApprovalStore(db)


def _cmd_serve(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    db_path = Path(args.data_dir) / "dbm.sqlite3"
    store = AuditStore(db_path)
    approvals = ApprovalStore(db_path)
    service = DbmService(config, store, approvals)
    service.metadata = MetadataCache(db_path, service.pool)
    mcp = build_mcp(service)

    try:
        if args.stdio:
            mcp.run(transport="stdio")
        else:
            from .admin import mount_admin

            mount_admin(mcp, service)
            mcp.run(transport="http", host=args.host, port=args.port)
    finally:
        service.close()


def _cmd_approvals(args: argparse.Namespace) -> None:
    store = _open_approvals(args.data_dir)
    status = None if args.status == "all" else args.status
    changes = store.list_by_status(status)
    if not changes:
        print("（无审批单）")
        return
    for c in changes:
        print(f"#{c.id} [{c.effective_status():9}] {c.risk_level:8} "
              f"{c.project}/{c.connection}({c.environment}) agent={c.agent}")
        print(f"    SQL: {c.sql[:100]}")
        if c.reason:
            print(f"    原因: {c.reason}")
        if c.risk_report.get("reasons"):
            print(f"    判定: {'; '.join(c.risk_report['reasons'])}")
    store.close()


def _cmd_decide(args: argparse.Namespace, approve: bool) -> None:
    store = _open_approvals(args.data_dir)
    try:
        change = (store.approve if approve else store.reject)(args.change_id, args.by, args.note)
    except ApprovalError as e:
        sys.exit(f"失败: {e}")
    verb = "已批准" if approve else "已拒绝"
    print(f"审批单 #{change.id} {verb}（审批人 {change.decided_by}）")
    print(json.dumps({"sql": change.sql, "connection": f"{change.project}/{change.connection}",
                      "risk": change.risk_level}, ensure_ascii=False, indent=2))
    store.close()


def main() -> None:
    argv = sys.argv[1:]
    # 向后兼容：无子命令时默认 serve
    if not argv or argv[0] not in _SUBCOMMANDS and argv[0] not in ("-h", "--help"):
        argv = ["serve", *argv]
    args = _build_parser().parse_args(argv)

    if args.cmd == "serve":
        _cmd_serve(args)
    elif args.cmd == "approvals":
        _cmd_approvals(args)
    elif args.cmd == "approve":
        _cmd_decide(args, approve=True)
    elif args.cmd == "reject":
        _cmd_decide(args, approve=False)


if __name__ == "__main__":
    main()
