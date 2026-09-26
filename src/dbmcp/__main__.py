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

DEFAULT_HOST = os.environ.get("DBM_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("DBM_PORT", "8100"))

# 两种运行形态，默认路径不同：
# - 源码目录里跑（有 config/ 目录）：配置 config/connections.yaml、数据 data/，与 launchd
#   脚本、文档里的相对路径一致；
# - pipx / uvx 装的命令行（没有仓库）：一切放 ~/.config/db-manage-mcp/ 下，与 env 文件、
#   keyring service 名同处一个目录。DBM_HOME 可整体挪走（测试用）。
_TEMPLATE_DEMO_DB = "./data/demo/shop.sqlite3"   # 模板里示例库的相对写法，首次生成时换成绝对路径


def user_dir() -> Path:
    return Path(os.environ.get("DBM_HOME") or Path.home() / ".config" / "db-manage-mcp")


def _repo_layout() -> bool:
    return Path("config").is_dir()


def default_config_path() -> str:
    if os.environ.get("DBM_CONFIG"):
        return os.environ["DBM_CONFIG"]
    return "config/connections.yaml" if _repo_layout() else str(user_dir() / "connections.yaml")


def default_data_dir() -> str:
    if os.environ.get("DBM_DATA_DIR"):
        return os.environ["DBM_DATA_DIR"]
    return "data" if _repo_layout() else str(user_dir() / "data")


def env_file_path() -> Path:
    return Path(os.environ.get("DBM_ENV_FILE") or user_dir() / "env")


def load_env_file(path: Path | None = None) -> int:
    """把 KEY=VALUE 形式的 env 文件注入环境（已设置的不覆盖），返回注入条数。

    与 scripts/dbm-serve.sh 读同一个文件：launchd 启动靠脚本注入，直接 `quay serve`
    则靠这里，两条路看到同一份 DBM_ADMIN_TOKEN / 数据库密码。
    """
    path = path or env_file_path()
    if not path.is_file():
        return 0
    n = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            n += 1
    return n


def ensure_config(path: str | Path, data_dir: str | Path) -> bool:
    """配置文件不存在就从包内模板生成（返回是否生成了）。

    模板里示例库写的是相对路径，落盘时换成 data_dir 下的绝对路径——uvx 装的命令行
    没有「项目目录」这个概念，相对路径会随启动时的 cwd 漂移。
    """
    path = Path(path)
    if path.exists():
        return False
    from importlib.resources import files

    template = (files("dbmcp") / "connections.example.yaml").read_text(encoding="utf-8")
    demo_db = (Path(data_dir).resolve() / "demo" / "shop.sqlite3").as_posix()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(template.replace(_TEMPLATE_DEMO_DB, demo_db), encoding="utf-8")
    return True


def persist_admin_token(token: str, path: Path | None = None) -> bool:
    """把首次生成的管理 token 写进 env 文件（600），下次启动沿用；写不了就返回 False。

    文件里已有 DBM_ADMIN_TOKEN= 行就原地替换而不是再追加一行——两行同名值对读文件的人是误导。
    """
    path = path or env_file_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        new_line = f"DBM_ADMIN_TOKEN={token}"
        replaced = False
        for i, line in enumerate(lines):
            if line.strip().removeprefix("export ").startswith("DBM_ADMIN_TOKEN="):
                lines[i] = new_line
                replaced = True
        if not replaced:
            lines.append(new_line)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        path.chmod(0o600)
        return True
    except OSError:
        return False

_SUBCOMMANDS = {"serve", "approvals", "approve", "reject"}


def _add_data_dir(p: argparse.ArgumentParser) -> None:
    p.add_argument("--data-dir", default=default_data_dir(), help="SQLite 数据目录")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dbm", description="Quay 数据库工作台 服务与审批 CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="运行 MCP 服务（默认子命令）")
    serve.add_argument("--config", default=default_config_path(),
                       help="连接配置 YAML 路径（不存在则首次启动生成示例配置）")
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
        f"  连接配置  {Path(args.config).resolve()}（{n_conn} 条连接）"
        + ("  ← 首次启动已生成示例配置" if getattr(args, "_config_created", False) else ""),
        f"  数据目录  {Path(args.data_dir).resolve()}",
    ]
    if token_note:
        lines.append("  " + token_note)
    return "\n" + "\n".join(lines) + "\n"


def _cmd_serve(args: argparse.Namespace) -> None:
    from .inbox import InboxNotifier, InboxStore
    from .notify import NotifierRouter, build_from_settings
    from .settings import SettingsStore

    load_env_file()   # ~/.config/db-manage-mcp/env：DBM_ADMIN_TOKEN 与 env:// 引用的密码
    args._config_created = ensure_config(args.config, args.data_dir)
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
                # 未设置则生成一个并存进 env 文件（600），下次启动沿用；存不了就只打印这一次。
                # 明文只在交互终端里打（人正等着登录）；stderr 进了日志文件（launchd）就只给路径，
                # 免得 token 躺在权限更宽的日志里。
                saved = persist_admin_token(admin_token)
                show = admin_token if (sys.stderr.isatty() or not saved) else "（见 env 文件）"
                if saved:
                    token_note = (f"登录 token  {show}\n"
                                  f"              已保存到 {env_file_path()}，下次启动沿用")
                else:
                    token_note = f"登录 token  {show}（本次随机生成，未能保存到 {env_file_path()}）"
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
