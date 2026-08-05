#!/usr/bin/env bash
# 审批等待闭环 e2e 的运行器：起一个隔离的测试实例再跑 e2e_approval_wait.py。
#
# 隔离要点（CLAUDE.md 规定）：端口 8201 + 临时数据目录，绝不用 8100——那是 launchd 正式
# 实例、也是 MCP 客户端连的端口，共用会抢端口并污染正式的审计/审批数据。
# --no-auth 只是省掉测试里处理 token/cookie（Host 校验仍在）。
set -uo pipefail
cd "$(dirname "$0")/.."

DATA="$(mktemp -d -t dbm-e2e-approval)"
set -a; . "${DBM_ENV_FILE:-$HOME/.config/db-manage-mcp/env}"; set +a
export NO_PROXY='*' no_proxy='*'   # 本机请求绕过 shell 里的 SOCKS 代理

uv run dbm serve --port 8201 --data-dir "$DATA" --no-auth > "$DATA/serve.log" 2>&1 &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null; rm -rf "$DATA"' EXIT

for _ in $(seq 1 40); do
  curl -s --noproxy '*' -o /dev/null "http://127.0.0.1:8201/admin/approvals" && break
  sleep 0.5
done

uv run python scripts/e2e_approval_wait.py
RC=$?
[ $RC -ne 0 ] && { echo "--- serve.log 尾部 ---"; tail -20 "$DATA/serve.log"; }
exit $RC
