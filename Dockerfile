FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# openssh-client 为 M2 的 SSH 多跳隧道预留
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src ./src
RUN uv sync --frozen --no-dev

# 容器内监听 0.0.0.0，宿主侧由 compose 限制为 127.0.0.1
ENV DBM_HOST=0.0.0.0 \
    DBM_CONFIG=/app/config/connections.yaml \
    DBM_DATA_DIR=/app/data

EXPOSE 8100
CMD ["uv", "run", "--no-sync", "dbm"]
