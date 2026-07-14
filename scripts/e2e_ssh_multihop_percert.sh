#!/usr/bin/env bash
# SSH 多跳「每跳不同证书」真实 e2e：host → jump1(密钥A) → jump2(密钥B) → MySQL
#
# 验证新的结构化 jump_hosts + 自动生成 `ssh -F` 配置：每个 Host 块带自己的 IdentityFile 与
# UserKnownHostsFile，ProxyJump 串链。命令行 `-i`/`-o` 只作用于最终目标、给不了每跳独立证书——
# 这正是必须真实 e2e 的原因（SQLite/单测测不出）。
# 正向：两跳各用各自正确的密钥 → 查询成功，且确认走生成的临时配置文件。
# 负向：把 jump2 的证书换成密钥A（jump2 只认 B）→ 隧道必须失败（证明每跳证书真被分别使用）。
#
# 前置：Docker 运行中；127.0.0.1:3306 有可用 MySQL（root/123456，或改下方变量）。
# 凭证隔离：临时目录生成两对一次性密钥，各自只授权到对应跳板；known_hosts 落临时目录，不碰 ~/.ssh。
set -euo pipefail

MYSQL_HOST="${MYSQL_HOST:-host.docker.internal}"   # jump2 视角的 DB 地址
MYSQL_PORT="${MYSQL_PORT:-3306}"
MYSQL_USER="${MYSQL_USER:-root}"
MYSQL_PASSWORD="${MYSQL_PASSWORD:-123456}"
JUMP1_PORT="${JUMP1_PORT:-2203}"

NET=dbm-ssh-pc-e2e
WORK=$(mktemp -d /tmp/dbm-ssh-pc-e2e.XXXXXX)
echo "临时目录: $WORK"

cleanup() {
  docker rm -f dbm-pc-jump1 dbm-pc-jump2 >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# 1. 两对一次性密钥：A 给 jump1，B 给 jump2
ssh-keygen -t ed25519 -N "" -f "$WORK/keyA" -q
ssh-keygen -t ed25519 -N "" -f "$WORK/keyB" -q
PUBA=$(cat "$WORK/keyA.pub")
PUBB=$(cat "$WORK/keyB.pub")

# 2. sshd 镜像（构建参数注入各自授权公钥）
docker network create "$NET" >/dev/null
cat > "$WORK/Dockerfile" <<'DF'
FROM alpine:3.20
RUN apk add --no-cache openssh \
    && ssh-keygen -A \
    && adduser -D -s /bin/sh tester \
    && passwd -u tester \
    && mkdir -p /home/tester/.ssh
ARG PUBKEY
RUN echo "$PUBKEY" > /home/tester/.ssh/authorized_keys \
    && chown -R tester:tester /home/tester/.ssh \
    && chmod 700 /home/tester/.ssh && chmod 600 /home/tester/.ssh/authorized_keys
EXPOSE 22
CMD ["/usr/sbin/sshd", "-D", "-e", \
     "-o", "AllowTcpForwarding=yes", "-o", "PasswordAuthentication=no", \
     "-o", "PermitRootLogin=no"]
DF
docker build -q -t dbm-sshd-pc-a --build-arg PUBKEY="$PUBA" "$WORK" >/dev/null
docker build -q -t dbm-sshd-pc-b --build-arg PUBKEY="$PUBB" "$WORK" >/dev/null

docker run -d --name dbm-pc-jump1 --network "$NET" \
  -p "127.0.0.1:${JUMP1_PORT}:22" dbm-sshd-pc-a >/dev/null   # 只认密钥A
# jump2 从其视角转发到宿主 MySQL：加 host-gateway 让容器能解析 host.docker.internal
docker run -d --name dbm-pc-jump2 --network "$NET" \
  --add-host host.docker.internal:host-gateway dbm-sshd-pc-b >/dev/null  # 只认密钥B

sleep 2

# 3. 预置 known_hosts：直接取每个容器的主机公钥，按 ssh 匹配格式写入
#    （jump2 仅内网可达，无法从 host 直接 keyscan，改用 docker exec 取公钥）
J1KEY=$(docker exec dbm-pc-jump1 cat /etc/ssh/ssh_host_ed25519_key.pub | awk '{print $1, $2}')
J2KEY=$(docker exec dbm-pc-jump2 cat /etc/ssh/ssh_host_ed25519_key.pub | awk '{print $1, $2}')
{
  echo "[127.0.0.1]:${JUMP1_PORT} ${J1KEY}"   # 第一跳：本地发布端口
  echo "dbm-pc-jump2 ${J2KEY}"                    # 第二跳：容器名（jump2 视角默认 22）
} > "$WORK/kh"

# 4. 经 dbmcp 的 EnginePool 用结构化 jump_hosts 建两跳隧道（每跳自带 key + known_hosts）
cd "$(dirname "$0")/.."
env NO_PROXY='*' no_proxy='*' uv run python - \
  "$WORK" "$JUMP1_PORT" "$MYSQL_HOST" "$MYSQL_PORT" "$MYSQL_USER" "$MYSQL_PASSWORD" <<'PYEOF'
import subprocess, sys
work, jump1_port, db_host, db_port, db_user, db_pw = sys.argv[1:7]

from dbmcp.config import ConnectionConfig
from dbmcp.engines import EnginePool, run_query
from dbmcp.tunnel import TunnelError

def make_cfg(hop2_key):
    return ConnectionConfig(
        engine="mysql", host=db_host, port=int(db_port), environment="dev",
        user=db_user, password=f"plain://{db_pw}",
        jump_hosts=[
            {"host": "127.0.0.1", "port": int(jump1_port), "user": "tester",
             "key_path": f"{work}/keyA", "known_hosts_path": f"{work}/kh"},   # 第一跳：密钥A
            {"host": "dbm-pc-jump2", "user": "tester",
             "key_path": hop2_key, "known_hosts_path": f"{work}/kh"},         # 第二跳：密钥B
        ],
    )

# 正向：jump2 用正确的密钥B
pool = EnginePool()
try:
    engine = pool.get("e2e", "percert", make_cfg(f"{work}/keyB"))
    r = run_query(engine, "SELECT version(), 42 AS answer", max_rows=1)
    print(f"✓ 每跳不同证书两跳隧道查询成功: version={r.rows[0][0]}, answer={r.rows[0][1]}")
    out = subprocess.run(["pgrep", "-fl", "ssh .*-F .*dbm-ssh-"],
                         capture_output=True, text=True).stdout
    assert "-F" in out, f"未走生成的 ssh 配置文件: {out!r}"
    print("✓ 确认走隧道生成的临时 ssh 配置（每跳独立 IdentityFile/UserKnownHostsFile）")
finally:
    pool.dispose()

# 负向：jump2 换成密钥A（jump2 只认 B）→ 必须失败，证明第二跳真用了自己的证书
pool2 = EnginePool()
failed = False
try:
    pool2.get("e2e", "percert-neg", make_cfg(f"{work}/keyA"))
except Exception as e:
    failed = True
    print(f"✓ 负向：第二跳用错证书如期失败 → {str(e).splitlines()[0][:80]}")
finally:
    pool2.dispose()
assert failed, "负向用例竟然连上了——每跳证书未被分别使用！"
print("✓ 每跳证书隔离验证通过")
PYEOF

echo "=== SSH 多跳「每跳不同证书」e2e 通过 ==="
