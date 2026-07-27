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

# 自动继承 macOS 系统代理（launchd 不继承 shell 环境；AI 直连 Anthropic/OpenAI 常需代理）。
# env 文件里已显式设置的代理变量优先，不覆盖。仅 macOS 生效；其它平台跳过。
if [[ "$(uname -s)" == "Darwin" ]] && command -v scutil >/dev/null 2>&1; then
  _sys_proxy_kv=$(scutil --proxy 2>/dev/null | awk '
    /HTTPSEnable *: *1/ { https_on=1 }
    /HTTPEnable *: *1/  { http_on=1 }
    /HTTPSProxy *:/     { https_host=$3 }
    /HTTPSPort *:/      { https_port=$3 }
    /HTTPProxy *:/      { http_host=$3 }
    /HTTPPort *:/       { http_port=$3 }
    END {
      if (https_on && https_host) printf "HTTPS_PROXY=http://%s:%s\n", https_host, https_port
      if (http_on  && http_host)  printf "HTTP_PROXY=http://%s:%s\n",  http_host,  http_port
    }')
  if [[ -n "$_sys_proxy_kv" ]]; then
    while IFS='=' read -r k v; do
      [[ -z "$k" ]] && continue
      # 已设置（env 文件或外部注入）就不覆盖，让人工配置优先
      if [[ -z "${!k-}" ]]; then export "$k=$v"; fi
    done <<< "$_sys_proxy_kv"
    # NO_PROXY 兜底：本机常见回环 + 私网段，别让 DB/管理页请求绕代理
    : "${NO_PROXY:=127.0.0.1,localhost,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16}"
    export NO_PROXY
  fi
fi

cd "$PROJECT_DIR"
exec uv run --no-sync dbm serve \
  --host "${DBM_HOST:-127.0.0.1}" \
  --port "${DBM_PORT:-8100}"
