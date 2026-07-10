# db-manage-mcp

为所有 agent 提供数据库访问的 MCP 服务：按项目管理连接与账密、SSH 多层跳板、SQL 审计与人工授权、操作审计与管理后台。设计文档见 [DESIGN.md](DESIGN.md)。

当前进度：**M1**（只读查询 + schema 探索 + 操作审计）。写操作授权流（M3）尚未上线，所有数据变更语句会被拒绝。

## 快速开始

```bash
# 1. 准备配置
cp config/connections.example.yaml config/connections.yaml   # 按需修改
cp .env.example .env                                          # 填写密码（env:// 引用）

# 2a. Docker 部署（推荐，daemon 常驻）
docker compose up -d --build
# MCP 端点: http://127.0.0.1:8100/mcp （streamable HTTP）

# 2b. 本地开发运行
uv sync --extra keyring
uv run dbm                 # HTTP daemon，默认 127.0.0.1:8100
uv run dbm --stdio         # stdio 模式，供单 agent 直连
```

Claude Code 中注册：

```bash
claude mcp add --transport http dbm http://127.0.0.1:8100/mcp
```

## MCP 工具

| 工具 | 说明 |
|---|---|
| `list_projects` / `list_connections` | 浏览可用连接（不含账号密码） |
| `query(project, connection, sql)` | 只读 SQL；非只读语句一律拒绝并审计 |
| `list_tables` / `describe_table` / `sample_rows` | schema 探索 |
| `test_connection` | 连通性检查 |

## 安全模型（M1 已实现的部分）

- **默认拒绝**：sqlglot AST 分类，解析失败/多语句/CTE 夹带 DML/`SELECT FOR UPDATE` 等一律拒绝
- **数据库层第二道防线**：MySQL `SESSION TRANSACTION READ ONLY`、PG `default_transaction_read_only=on`、SQLite `PRAGMA query_only`
- **密钥不落明文**：配置只存 `env://` / `keyring://` 引用，密码不进日志与工具返回值
- **全量操作审计**：每次调用（含被拒绝的）记录 agent、时间、连接、SQL、行数、耗时到 SQLite

## 开发

```bash
uv run pytest        # 全量测试
```
