# db-manage-mcp

为所有 agent 提供数据库访问的 MCP 服务：按项目管理连接与账密、SSH 多层跳板、SQL 审计与人工授权、操作审计与管理后台。设计文档见 [DESIGN.md](DESIGN.md)。

当前进度：**M3 完成**。已支持只读查询、schema 探索、SSH 多跳、元数据缓存、SQL 风险审计、拒绝—重提人工授权、操作审计与管理后台。Redis、elicitation 快捷审批、goInception 集成在 M4。

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
| `execute(project, connection, sql, reason?, change_id?)` | 写操作：首次提交生成审批单并返回 change_id；批准后带 change_id 重提执行 |
| `get_change_status(change_id)` | 查询审批单状态与风险报告 |
| `list_tables` / `describe_table` / `sample_rows` | schema 探索 |
| `test_connection` | 连通性检查 |

## 管理后台

daemon 运行时同端口提供（默认 http://127.0.0.1:8100）：

- `/admin/approvals` — 审批中心：待审列表 + 详情页（SQL、风险报告、批准/拒绝）
- `/admin/audit` — 操作审计：按状态/连接筛选

## 写操作授权流程

1. agent 调 `execute` 提交写 SQL → 系统评估风险、生成审批单、**拒绝**并返回 `change_id`
2. 人在 `/admin/approvals` 查看风险报告后批准/拒绝
3. 批准后 agent 带 `change_id` 重提**相同 SQL** → 执行（执行的是审批单里存储的 SQL，重提文本仅作指纹校验）
4. 被拒则返回人的理由，agent 调整后重新发起

## 安全模型（M1 已实现的部分）

- **默认拒绝**：sqlglot AST 分类，解析失败/多语句/CTE 夹带 DML/`SELECT FOR UPDATE` 等一律拒绝
- **数据库层第二道防线**：MySQL `SESSION TRANSACTION READ ONLY`、PG `default_transaction_read_only=on`、SQLite `PRAGMA query_only`
- **密钥不落明文**：配置只存 `env://` / `keyring://` 引用，密码不进日志与工具返回值
- **全量操作审计**：每次调用（含被拒绝的）记录 agent、时间、连接、SQL、行数、耗时到 SQLite

## 开发

```bash
uv run pytest        # 全量测试
```
