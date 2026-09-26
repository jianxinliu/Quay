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
import secrets
import sys
from pathlib import Path

from .approvals import ApprovalError, ApprovalStore
from .audit.log import AuditStore
from .config import load_config
from .metadata import MetadataCache
from .server import build_mcp
from .service import DbmService
from .snippets import SnippetStore

DEFAULT_CONFIG = os.environ.get("DBM_CONFIG", "config/connections.yaml")
DEFAULT_DATA_DIR = os.environ.get("DBM_DATA_DIR", "data")
DEFAULT_HOST = os.environ.get("DBM_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("DBM_PORT", "8100"))

_SUBCOMMANDS = {"serve", "approvals", "approve", "reject"}


def _add_data_dir(p: argparse.ArgumentParser) -> None:
    p.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="SQLite 数据目录")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dbm", description="Quay 数据库工作台 服务与审批 CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="运行 MCP 服务（默认子命令）")
    serve.add_argument("--config", default=DEFAULT_CONFIG, help="连接配置 YAML 路径")
    _add_data_dir(serve)
    serve.add_argument("--stdio", action="store_true", help="以 stdio 传输运行（默认 HTTP daemon）")
    serve.add_argument("--host", default=DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--retention-days", type=int,
                       default=int(os.environ.get("DBM_RETENTION_DAYS", "30")),
                       help="审计记录与终态审批单保留天数（默认 30）")
    serve.add_argument("--no-auth", action="store_true",
                       help=argparse.SUPPRESS)  # 仅供本机测试脚手架，不对外

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


def _warm_tokenizer(data_dir: str) -> None:
    """后台预热 token 分词器（装了 tiktoken 才有效）。

    首次加载要下 3.4MB 词表，不该让某一次查询替所有人承担这几秒；下不到也无所谓，
    budget 会回退到字符类别估算并在界面上标「粗估」。词表缓存钉在数据目录，
    否则默认落在 TMPDIR、被 macOS 清理后又要重下一次。
    """
    import threading

    from .budget import set_tokenizer_cache_dir, warm_tokenizer

    cache = Path(data_dir) / "tokenizer"
    cache.mkdir(parents=True, exist_ok=True)
    set_tokenizer_cache_dir(str(cache))
    threading.Thread(target=warm_tokenizer, name="dbm-tokenizer",
                     daemon=True).start()


def _open_approvals(data_dir: str) -> ApprovalStore:
    db = Path(data_dir) / "dbm.sqlite3"
    if not db.exists():
        sys.exit(f"数据文件不存在: {db}（daemon 还没运行过？用 --data-dir 指定目录）")
    return ApprovalStore(db)


def _startup_banner(args: argparse.Namespace, config, token_note: str) -> str:  # noqa: ANN001
    """启动时打给人看的一段话：版本、后台/MCP 地址、配置与数据在哪。

    替代 FastMCP 自带的横幅（那是它自己的品牌与升级提醒），三种认证分支都打印，
    否则设置了 DBM_ADMIN_TOKEN 的正常启动反而什么都不说。
    """
    from . import __version__

    host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    n_conn = sum(len(p.connections) for p in config.projects.values())
    lines = [
        f"Quay {__version__}",
        f"  管理后台  http://{host}:{args.port}/admin",
        f"  MCP 端点  http://{host}:{args.port}/mcp",
        f"  连接配置  {Path(args.config).resolve()}（{n_conn} 条连接）",
        f"  数据目录  {Path(args.data_dir).resolve()}",
    ]
    if token_note:
        lines.append("  " + token_note)
    return "\n" + "\n".join(lines) + "\n"


def _cmd_serve(args: argparse.Namespace) -> None:
    from .inbox import InboxNotifier, InboxStore
    from .notify import NotifierRouter, build_from_settings
    from .settings import SettingsStore

    config = load_config(args.config)
    db_path = Path(args.data_dir) / "dbm.sqlite3"
    store = AuditStore(db_path)
    approvals = ApprovalStore(db_path)

    # 通知：内推（管理后台铃铛，恒开）+ 主外部渠道（配置里选一个）+ 可选 macOS 本地
    # NotifierRouter 每次 send 前读最新 settings 组装，改配置即时生效不需重启
    inbox_store = InboxStore(db_path)
    settings_store = SettingsStore(db_path)
    inbox_notifier = InboxNotifier(inbox_store)

    def _make_notifier():
        return build_from_settings(settings_store.get_all(), inbox=inbox_notifier)

    service = DbmService(config, store, approvals, config_path=args.config,
                         notifier=NotifierRouter(_make_notifier))
    service.inbox = inbox_store
    service.settings = settings_store
    service.metadata = MetadataCache(db_path, service.pool)
    service.snippets = SnippetStore(db_path)
    from .analysis import AnalysisStore
    from .examples import seed_examples
    from .workflows import WorkflowRunStore, WorkflowScheduleStore, WorkflowStore
    service.analysis = AnalysisStore(Path(args.data_dir) / "analysis")
    service.workflows = WorkflowStore(db_path)
    service.schedules = WorkflowScheduleStore(db_path)
    service.runs = WorkflowRunStore(db_path)
    service.data_dir = args.data_dir  # xlsx 产物落到 data_dir/workflow_runs/{run_id}/
    public_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    service.base_url = f"http://{public_host}:{args.port}"
    seed_examples(service.workflows, args.data_dir)  # 首次启动播种示例 workflow
    _warm_tokenizer(args.data_dir)
    service.start_housekeeping(retention_days=args.retention_days)
    service.start_scheduler(interval_s=30)  # 每 30s tick 一次；对齐下拉最小 1 分钟粒度
    mcp = build_mcp(service)

    try:
        if args.stdio:
            mcp.run(transport="stdio", show_banner=False)
        else:
            from .admin import mount_admin

            admin_token = os.environ.get("DBM_ADMIN_TOKEN") or secrets.token_urlsafe(24)
            if args.no_auth:
                token_note = "登录      --no-auth 模式，后台无需登录（仅供本机测试）"
            elif not os.environ.get("DBM_ADMIN_TOKEN"):
                # 未设置则一次性生成，打印到 stderr 方便本地登录；常驻部署应显式注入
                token_note = f"登录 token  {admin_token}（本次随机生成；常驻请设置 DBM_ADMIN_TOKEN）"
            else:
                token_note = "登录 token  来自 DBM_ADMIN_TOKEN"
            print(_startup_banner(args, config, token_note), file=sys.stderr)
            mount_admin(mcp, service, admin_token, no_auth=args.no_auth)
            # show_banner=False：不打 FastMCP 的品牌横幅，也就不跑它的 PyPI 版本自检——
            # 那次自检在带 SOCKS 代理的 shell 里会因缺 socksio 直接把进程拖死（见 CLAUDE.md）
            mcp.run(transport="http", host=args.host, port=args.port, show_banner=False)
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
