#!/usr/bin/env bash
# db-manage-mcp 常驻启动脚本（供 launchd 调用，也可手动运行）。
# 密钥从 600 权限的 env 文件读取，不写进 plist（plist 在 ~/Library 下相对易读）。
set -euo pipefail

# launchd 的 PATH 极简，需补上 uv 常见位置
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${DBM_ENV_FILE:-$HOME/.config/db-manage-mcp/env}"

# 读取密钥与配置（DBM_MYSQL_PW / DBM_ADMIN_TOKEN / DBM_HOST / DBM_PORT 等）
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

cd "$PROJECT_DIR"
exec uv run --no-sync dbm serve \
  --host "${DBM_HOST:-127.0.0.1}" \
  --port "${DBM_PORT:-8100}"
