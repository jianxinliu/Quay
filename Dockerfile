# Quay（dbmcp）容器镜像 —— 用于懒猫微服（LazyCat）等反向代理部署。
# 多阶段构建：builder 阶段带编译器编译 C 扩展(zstd/lz4)，运行阶段只拷 .venv、不带编译器，
# 显著减小体积。项目原生定位本地进程模式；容器化用 env:// 密钥（无系统 keyring 后端），
# 配置/数据/密钥统一落在 $HOME=/lzcapp/var 下，由懒猫持久化卷托管。

# ---------- 构建阶段 ----------
FROM python:3.12-slim AS builder
# build-essential：clickhouse-driver 的 zstd/lz4 C 扩展在 arm64 无预编译 wheel，需就地编译。
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
WORKDIR /app
COPY . /app
# 只装运行期依赖（--no-dev），不装 keyring extra（容器无 keyring 后端）。产出 /app/.venv。
RUN uv sync --no-dev

# ---------- 运行阶段 ----------
FROM python:3.12-slim
# openssh-client：SSH 多跳隧道功能依赖系统 ssh；curl/ca-certificates：健康探测与 TLS。
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
# 从 builder 拷贝应用源码 + 已编译好的 .venv（两阶段同为 python:3.12-slim，解释器路径一致，venv 可用）。
COPY --from=builder /app /app

# 配置/数据/密钥统一落在持久化卷根 /lzcapp/var（$HOME 指向它，~/.config 自然持久化）。
ENV HOME=/lzcapp/var \
    DBM_HOST=0.0.0.0 \
    DBM_PORT=8100 \
    DBM_TRUSTED_PROXY_AUTH=1 \
    NO_PROXY=* \
    no_proxy=*

EXPOSE 8100

# entrypoint 负责 seed 持久化配置/数据目录后再起服务（直接用 venv 内 dbm 二进制，不走 `uv run`）。
COPY --from=builder /app/docker-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
