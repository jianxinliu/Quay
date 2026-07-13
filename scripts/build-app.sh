#!/usr/bin/env bash
# 生成一个可双击启动的 macOS .app（db-manage-mcp.app）：
#   双击 → 确保常驻服务已起（有 launchd 就 kickstart，没有就直接后台拉起）
#        → 等端口就绪 → 用默认浏览器打开管理后台。
#
# 用法：  bash scripts/build-app.sh [输出目录]
#   不传输出目录时生成到仓库根目录：./db-manage-mcp.app
#   常见做法：bash scripts/build-app.sh ~/Applications   （之后 Launchpad/Spotlight 可搜到）
#
# 说明：本地构建的 .app 没有 quarantine 标记，双击不触发 Gatekeeper 拦截（无需签名）。
#       图标：若存在 scripts/app-icon.png（建议 1024x1024）则自动生成 .icns，否则用系统默认图标。
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${1:-$PROJECT_DIR}"
APP="$OUT_DIR/db-manage-mcp.app"
MACOS="$APP/Contents/MacOS"
RES="$APP/Contents/Resources"

mkdir -p "$MACOS" "$RES"

# ---------- 1. 启动脚本（可执行文件） ----------
# PROJECT_DIR 在构建时写死为绝对路径：.app 移到别处仍能找到仓库；仓库整体搬家后请重跑本脚本。
cat > "$MACOS/db-manage-mcp" <<EOF
#!/bin/bash
# db-manage-mcp 启动器（由 build-app.sh 生成，勿手改；改需求改 scripts/build-app.sh 重生成）。
set -u
PROJECT_DIR="$PROJECT_DIR"
EOF
cat >> "$MACOS/db-manage-mcp" <<'EOF'
LABEL="com.db-manage-mcp"
UID_NUM="$(id -u)"
ENV_FILE="${DBM_ENV_FILE:-$HOME/.config/db-manage-mcp/env}"
LOG="$HOME/Library/Logs/db-manage-mcp.log"

# 端口：从 env 文件读 DBM_PORT，默认 8100
PORT=8100
if [ -f "$ENV_FILE" ]; then
  p="$(grep -E '^DBM_PORT=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d ' ')"
  [ -n "${p:-}" ] && PORT="$p"
fi
URL="http://127.0.0.1:$PORT/admin/login"

notify() { /usr/bin/osascript -e "display notification \"$1\" with title \"db-manage-mcp\"" >/dev/null 2>&1 || true; }
alert()  { /usr/bin/osascript -e "display dialog \"$1\" with title \"db-manage-mcp\" buttons {\"好\"} default button 1 with icon caution" >/dev/null 2>&1 || true; }
is_up()  { /usr/bin/curl -sf --noproxy '*' -o /dev/null "http://127.0.0.1:$PORT/admin/login"; }

if is_up; then
  open "$URL"; exit 0
fi

notify "正在启动服务…"
if /bin/launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then
  # 已装 launchd 常驻服务 → 直接拉起（KeepAlive 会保活）
  /bin/launchctl kickstart "gui/$UID_NUM/$LABEL" >/dev/null 2>&1 || true
else
  # 未装常驻服务 → 直接后台拉起（复用 dbm-serve.sh 的 env 注入与 PATH 处理）
  export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
  nohup /bin/bash "$PROJECT_DIR/scripts/dbm-serve.sh" >> "$LOG" 2>&1 </dev/null &
fi

# 等端口就绪（最多约 30 秒：uv run 首次可能要装依赖）
for _ in $(seq 1 60); do
  is_up && break
  sleep 0.5
done

if is_up; then
  notify "已启动，正在打开后台…"
  open "$URL"
else
  alert "服务启动超时。请查看日志：$LOG"
fi
EOF
chmod +x "$MACOS/db-manage-mcp"

# ---------- 2. Info.plist ----------
ICON_LINE=""
if [ -f "$PROJECT_DIR/scripts/app-icon.png" ] && command -v iconutil >/dev/null 2>&1; then
  ICONSET="$(mktemp -d)/icon.iconset"
  mkdir -p "$ICONSET"
  for s in 16 32 64 128 256 512; do
    sips -z "$s" "$s"     "$PROJECT_DIR/scripts/app-icon.png" --out "$ICONSET/icon_${s}x${s}.png"     >/dev/null 2>&1 || true
    d=$((s*2))
    sips -z "$d" "$d"     "$PROJECT_DIR/scripts/app-icon.png" --out "$ICONSET/icon_${s}x${s}@2x.png"  >/dev/null 2>&1 || true
  done
  if iconutil -c icns "$ICONSET" -o "$RES/appicon.icns" >/dev/null 2>&1; then
    ICON_LINE="  <key>CFBundleIconFile</key><string>appicon</string>"
  fi
fi

cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>db-manage-mcp</string>
  <key>CFBundleDisplayName</key><string>db-manage-mcp</string>
  <key>CFBundleIdentifier</key><string>com.db-manage-mcp.launcher</string>
  <key>CFBundleExecutable</key><string>db-manage-mcp</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
$ICON_LINE
</dict>
</plist>
EOF

# 去掉可能的 quarantine 标记（本地构建一般没有，保险起见）
xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true

echo "✓ 已生成: $APP"
echo "  双击即可启动服务并打开管理后台（http://127.0.0.1:8100/admin/login）。"
echo "  想放进启动台/Spotlight：bash scripts/build-app.sh ~/Applications"
[ -z "$ICON_LINE" ] && echo "  （用自定义图标：把 1024x1024 的 PNG 存为 scripts/app-icon.png 后重跑本脚本）"
exit 0
