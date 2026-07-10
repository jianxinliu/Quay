"""入口：dbm 命令。

默认以 streamable HTTP daemon 运行（Docker 部署形态）；--stdio 供单 agent 直连。
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .audit.log import AuditStore
from .config import load_config
from .server import build_mcp
from .service import DbmService

DEFAULT_CONFIG = os.environ.get("DBM_CONFIG", "config/connections.yaml")
DEFAULT_DATA_DIR = os.environ.get("DBM_DATA_DIR", "data")
DEFAULT_HOST = os.environ.get("DBM_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("DBM_PORT", "8100"))


def main() -> None:
    parser = argparse.ArgumentParser(prog="dbm", description="db-manage-mcp 服务")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="连接配置 YAML 路径")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="SQLite 数据目录")
    parser.add_argument("--stdio", action="store_true", help="以 stdio 传输运行（默认 HTTP daemon）")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    config = load_config(args.config)
    store = AuditStore(Path(args.data_dir) / "dbm.sqlite3")
    service = DbmService(config, store)
    mcp = build_mcp(service)

    try:
        if args.stdio:
            mcp.run(transport="stdio")
        else:
            mcp.run(transport="http", host=args.host, port=args.port)
    finally:
        service.close()


if __name__ == "__main__":
    main()
