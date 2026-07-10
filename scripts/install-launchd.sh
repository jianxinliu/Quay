#!/usr/bin/env bash
# 把 db-manage-mcp 安装为 macOS 常驻服务（launchd LaunchAgent）：
# 开机自启 + 崩溃自动拉起。密钥存 ~/.config/db-manage-mcp/env（600），不进 plist。
#
# 用法：  bash scripts/install-launchd.sh
# 卸载：  bash scripts/install-launchd.sh --uninstall
set -euo pipefail

LABEL="com.db-manage-mcp"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/db-manage-mcp.log"
ENV_DIR="$HOME/.config/db-manage-mcp"
ENV_FILE="$ENV_DIR/env"
UID_NUM="$(id -u)"

uninstall() {
  launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "已卸载常驻服务（保留 $ENV_FILE 与日志）。"
}

if [ "${1:-}" = "--uninstall" ]; then
  uninstall
  exit 0
fi

# 1. 密钥文件（首次生成模板，600 权限）
mkdir -p "$ENV_DIR"
if [ ! -f "$ENV_FILE" ]; then
  TOKEN="$(uv run python -c 'import secrets;print(secrets.token_urlsafe(24))' 2>/dev/null || echo "change-me-$RANDOM")"
  cat > "$ENV_FILE" <<EOF
# db-manage-mcp 常驻服务的密钥与配置（600 权限，勿提交）。
# 管理后台登录 token：
DBM_ADMIN_TOKEN=$TOKEN
# 配置里 env:// 引用的数据库密码，按需补充，例如：
# DBM_MYSQL_PW=your-mysql-password
# 可选：DBM_HOST=127.0.0.1  DBM_PORT=8100
EOF
  chmod 600 "$ENV_FILE"
  echo "已生成密钥文件: $ENV_FILE"
  echo "  管理 token: $TOKEN"
  echo "  如数据库用 env:// 引用密码，请在该文件补上（如 DBM_MYSQL_PW=...）后重启服务。"
else
  echo "复用已有密钥文件: $ENV_FILE"
fi

# 2. 生成 plist
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$PROJECT_DIR/scripts/dbm-serve.sh</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
  <key>WorkingDirectory</key><string>$PROJECT_DIR</string>
</dict>
</plist>
EOF
echo "已写入 plist: $PLIST"

# 3. 加载（bootout 旧的再 bootstrap，幂等）
PORT="${DBM_PORT:-8100}"
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
# 等旧实例释放端口，避免 bootout→bootstrap 竞态导致新实例绑定失败
for _ in $(seq 1 20); do
  if ! lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then break; fi
  sleep 0.3
done
if ! launchctl bootstrap "gui/$UID_NUM" "$PLIST"; then
  echo "bootstrap 失败，重试一次…" >&2
  sleep 1
  launchctl bootstrap "gui/$UID_NUM" "$PLIST"
fi
launchctl enable "gui/$UID_NUM/$LABEL"

# 确认真的起来了
sleep 2
if launchctl print "gui/$UID_NUM/$LABEL" 2>/dev/null | grep -q "state = running"; then
  :
else
  echo "⚠️ 服务未进入 running，检查日志: $LOG" >&2
fi

echo ""
echo "✓ 常驻服务已启动（开机自启 + 崩溃自动拉起）"
echo "  MCP:   http://127.0.0.1:8100/mcp"
echo "  后台:  http://127.0.0.1:8100/admin/login"
echo "  日志:  tail -f $LOG"
echo "  状态:  launchctl print gui/$UID_NUM/$LABEL | grep state"
echo "  卸载:  bash scripts/install-launchd.sh --uninstall"
