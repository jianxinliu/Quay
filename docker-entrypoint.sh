#!/bin/sh
# 懒猫容器启动：确保持久化目录与配置存在（都落在 /lzcapp/var 持久化卷，重启/升级不丢）。
set -e

mkdir -p /lzcapp/var/data

CONFIG=/lzcapp/var/connections.yaml
DEMO=/lzcapp/var/demo.sqlite3

# 首次启动：放入内置的 SQLite 示例库 + seed 一个指向它的连接配置，开箱即可查询。
# 之后用户在管理后台的增删连接都会写回 $CONFIG（随持久化卷保留），不再覆盖。
if [ ! -f "$CONFIG" ]; then
  [ -f "$DEMO" ] || cp /app/demo/demo.sqlite3 "$DEMO" 2>/dev/null || true
  cat > "$CONFIG" <<'YAML'
projects:
  demo:
    connections:
      demo-sqlite:
        engine: sqlite
        database: /lzcapp/var/demo.sqlite3
        environment: local
YAML
fi

exec /app/.venv/bin/dbm serve \
  --host 0.0.0.0 --port 8100 \
  --config "$CONFIG" \
  --data-dir /lzcapp/var/data
