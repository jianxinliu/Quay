#!/bin/sh
# 懒猫容器启动：确保持久化目录与配置存在（都落在 /lzcapp/var 持久化卷，重启/升级不丢）。
set -e

mkdir -p /lzcapp/var/data

CONFIG=/lzcapp/var/connections.yaml
# 首次启动 seed 一个空配置；连接由管理后台 UI 写入此文件（随持久化卷保留）。
if [ ! -f "$CONFIG" ]; then
  printf 'projects: {}\n' > "$CONFIG"
fi

exec /app/.venv/bin/dbm serve \
  --host 0.0.0.0 --port 8100 \
  --config "$CONFIG" \
  --data-dir /lzcapp/var/data
