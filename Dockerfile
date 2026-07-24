# Quay（dbmcp）容器镜像 —— 用于懒猫微服（LazyCat）等反向代理部署。
# 说明：项目原生定位本地进程模式；容器化时用 env:// 密钥（无系统 keyring 后端），
# 并把配置/数据/密钥统一落在 $HOME=/lzcapp/var 下，由懒猫持久化卷托管。
FROM python:3.12-slim

# openssh-client：SSH 多跳隧道功能依赖系统 ssh；curl/ca-certificates：健康探测与 TLS；
# build-essential：clickhouse-driver 的 zstd/lz4 C 扩展无预编译 wheel，需就地编译。
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client curl ca-certificates build-essential \
    && rm -rf /var/lib/apt/lists/*

# uv：与项目开发一致的依赖管理器
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
COPY . /app

# 只装运行期依赖（--no-dev），不装 keyring extra（容器无 keyring 后端）
RUN uv sync --no-dev

# 配置/数据/密钥统一落在持久化卷根 /lzcapp/var（$HOME 指向它，~/.config 自然持久化）
ENV HOME=/lzcapp/var \
    DBM_HOST=0.0.0.0 \
    DBM_PORT=8100 \
    DBM_TRUSTED_PROXY_AUTH=1 \
    NO_PROXY=* \
    no_proxy=*

EXPOSE 8100

# 数据目录放持久化卷；连接配置由管理后台写入 ~/.config/db-manage-mcp（随 $HOME 持久化）
CMD ["uv", "run", "dbm", "serve", "--host", "0.0.0.0", "--port", "8100", "--data-dir", "/lzcapp/var/data"]
