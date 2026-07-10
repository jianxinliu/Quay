#!/usr/bin/env bash
# SSH 多跳隧道真实 e2e：host → jump1(容器,发布端口) → jump2(容器,仅内网) → MySQL
#
# 前置：Docker 运行中；127.0.0.1:3306 有可用 MySQL（root/123456，或改下方变量）。
# 凭证完全隔离：临时目录生成一次性密钥对，known_hosts 也落在临时目录，不碰 ~/.ssh。
# 清理：脚本退出时删除容器与网络；临时密钥目录保留在 /tmp 供排查（打印路径）。
set -euo pipefail

MYSQL_HOST="${MYSQL_HOST:-host.docker.internal}"   # jump2 视角的 DB 地址
MYSQL_PORT="${MYSQL_PORT:-3306}"
MYSQL_USER="${MYSQL_USER:-root}"
MYSQL_PASSWORD="${MYSQL_PASSWORD:-123456}"
JUMP1_PORT="${JUMP1_PORT:-2201}"

NET=dbm-ssh-e2e
WORK=$(mktemp -d /tmp/dbm-ssh-e2e.XXXXXX)
echo "临时目录: $WORK"

cleanup() {
  docker rm -f dbm-jump1 dbm-jump2 >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# 1. 一次性密钥对
ssh-keygen -t ed25519 -N "" -f "$WORK/id_ed25519" -q
PUB=$(cat "$WORK/id_ed25519.pub")

# 2. 自建极简 sshd 镜像（alpine + openssh，开启 TCP 转发，密钥认证 only）
docker network create "$NET" >/dev/null
cat > "$WORK/Dockerfile" <<'DF'
FROM alpine:3.20
RUN apk add --no-cache openssh \
    && ssh-keygen -A \
    && adduser -D -s /bin/sh tester \
    && passwd -u tester \
    && mkdir -p /home/tester/.ssh \
    && true
ARG PUBKEY
RUN echo "$PUBKEY" > /home/tester/.ssh/authorized_keys \
    && chown -R tester:tester /home/tester/.ssh \
    && chmod 700 /home/tester/.ssh && chmod 600 /home/tester/.ssh/authorized_keys
EXPOSE 22
# -o 命令行选项优先级最高，避免镜像自带 sshd_config 里 AllowTcpForwarding no 生效
CMD ["/usr/sbin/sshd", "-D", "-e", \
     "-o", "AllowTcpForwarding=yes", "-o", "PasswordAuthentication=no", \
     "-o", "PermitRootLogin=no"]
DF
docker build -q -t dbm-sshd-e2e --build-arg PUBKEY="$PUB" "$WORK" >/dev/null

docker run -d --name dbm-jump1 --network "$NET" \
  -p "127.0.0.1:${JUMP1_PORT}:22" dbm-sshd-e2e >/dev/null
docker run -d --name dbm-jump2 --network "$NET" dbm-sshd-e2e >/dev/null

sleep 2

# 3. 独立 ssh 配置文件（-F 对跳板链上每一跳都生效；命令行 -o 只作用于最终目标）
cat > "$WORK/ssh_config" <<EOF
Host *
  IdentityFile $WORK/id_ed25519
  UserKnownHostsFile $WORK/known_hosts
  StrictHostKeyChecking accept-new
EOF

# 4. 经 dbmcp 的 EnginePool 建两跳隧道并查询
cd "$(dirname "$0")/.."
env NO_PROXY='*' no_proxy='*' uv run python - "$WORK" "$JUMP1_PORT" "$MYSQL_HOST" "$MYSQL_PORT" "$MYSQL_USER" "$MYSQL_PASSWORD" <<'PYEOF'
import subprocess, sys
work, jump1_port, db_host, db_port, db_user, db_pw = sys.argv[1:7]

from dbmcp.config import ConnectionConfig
from dbmcp.engines import EnginePool, run_query

cfg = ConnectionConfig(
    engine="mysql",
    host=db_host, port=int(db_port),
    environment="dev",
    user=db_user, password=f"plain://{db_pw}",
    jump_hosts=[f"tester@127.0.0.1:{jump1_port}", "tester@dbm-jump2"],
    ssh_options=["-F", f"{work}/ssh_config"],
)
pool = EnginePool()
try:
    engine = pool.get("e2e", "multihop", cfg)
    r = run_query(engine, "SELECT version(), 42 AS answer", max_rows=1)
    print(f"✓ 两跳隧道查询成功: version={r.rows[0][0]}, answer={r.rows[0][1]}")
    # 验证确实走了 -J 链
    out = subprocess.run(["pgrep", "-fl", "ssh .*-J"], capture_output=True, text=True).stdout
    assert "dbm-jump2" in out and f"127.0.0.1:{jump1_port}" in out.replace("tester@", ""), out
    print(f"✓ ssh 进程确认走 ProxyJump 链: ...{out.strip().split('ssh')[-1][:90]}")
finally:
    pool.dispose()
print("✓ 隧道与引擎已回收")
PYEOF

echo "=== SSH 多跳 e2e 通过 ==="