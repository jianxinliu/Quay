# db-manage-mcp

为所有 agent 提供数据库访问的 MCP 服务：按项目管理连接与账密、SSH 多层跳板、SQL 审计与人工授权、操作审计与管理后台。设计文档见 [DESIGN.md](DESIGN.md)。

功能：只读查询、schema 探索、SSH 多跳、元数据缓存、SQL/Redis 风险审计、拒绝—重提人工授权（管理后台 / elicitation 会话内确认 / CLI 三种审批通道）、敏感字段脱敏、操作审计、连接管理后台。**本地进程模式部署**（不用 Docker——本地单机场景 Docker 只带来麻烦：连宿主库要绕网络、keyring 用不了导致连接管理页残废、SSH key 变容器内路径）。

## 快速开始

```bash
uv sync --extra keyring
cp config/connections.example.yaml config/connections.yaml   # 按需修改

# 前台运行（开发/调试）
DBM_MYSQL_PW=... DBM_ADMIN_TOKEN=你的token uv run dbm serve
# stdio 模式（单 agent 直连）
uv run dbm serve --stdio
```

MCP 端点 `http://127.0.0.1:8100/mcp`，管理后台 `http://127.0.0.1:8100/admin/login`。

### 常驻服务（macOS launchd，开机自启 + 崩溃自动拉起）

```bash
bash scripts/install-launchd.sh          # 安装并启动，首次会生成管理 token
# 密钥写在 ~/.config/db-manage-mcp/env（600 权限），按需补 DBM_MYSQL_PW=...
bash scripts/install-launchd.sh          # 改配置/密钥后重跑即热重启（幂等）
bash scripts/install-launchd.sh --uninstall   # 卸载
tail -f ~/Library/Logs/db-manage-mcp.log      # 看日志
```

> 崩溃后 launchd 自动拉起，但有约 10 秒重启节流（正常行为）。

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
| `redis_command(project, connection, command, ...)` | Redis：读命令直通；写命令走授权流程；FLUSHDB/KEYS/EVAL 等按 CRITICAL 管控 |
| `list_tables` / `describe_table` / `sample_rows` | schema 探索 |
| `test_connection` | 连通性检查 |

## 三种审批通道

1. **elicitation 会话内确认**（local/dev 默认开，staging/prod 默认关，`policy.elicitation_approval` 可配）：客户端支持时直接弹确认，批准即执行；不支持则自动回退审批单
2. **管理后台** `/admin/approvals`：查看完整风险报告后决策
3. **CLI 兜底**：`uv run dbm approvals` / `uv run dbm approve <id> --note ok` / `uv run dbm reject <id> --note 理由`

无论哪种通道，审批单都会创建并留痕（谁批的、何时、备注）。

## 资源与数据治理

- **连接/隧道空闲回收**：引擎与 SSH 隧道空闲 10 分钟自动回收，断开的隧道按需重建
- **审计保留期**：审计记录与终态审批单默认保留 30 天（`--retention-days` / `DBM_RETENTION_DAYS` 可配），后台每分钟清理一轮
- **大单元格截断**：超过 `policy.max_cell_chars`（默认 4096 字符）的单元格截断并标注原始长度，防止大 TEXT/BLOB 撑爆 agent 上下文
- **分页交给 agent**：查询结果超出 `max_rows` 时返回 `truncated: true` + 提示，agent 用 LIMIT/OFFSET 自行翻页；管理后台审计页支持 offset/limit 翻页
- **EXPLAIN 进审批单**：写操作的执行计划（不带 ANALYZE）自动附进风险报告，供审批人参考
- **SSH 多跳**：`jump_hosts` 按序多级跳板；自定义密钥/known_hosts 用 `ssh_options: ["-F", "/path/ssh_config"]`（注意 `-o` 不会传递给跳板连接）；真实两跳验证脚本 `scripts/e2e_ssh_multihop.sh`

## 敏感字段脱敏

查询结果按列名自动脱敏：内置模式（password/passwd/secret/token/api_key/credit_card 等子串匹配，`policy.mask_default_patterns: false` 可关）+ `policy.mask_columns` 业务自定义列，命中的值替换为 `***MASKED***`，响应带 `masked_columns` 说明。

## 管理后台

daemon 运行时同端口提供（默认 http://127.0.0.1:8100），**需登录**：

- 登录 token 由 `DBM_ADMIN_TOKEN` 注入；未设置则启动时生成随机 token 打印到 stderr
- `/admin/approvals` — 审批中心：待审列表 + 详情页（SQL、风险报告、EXPLAIN、批准/拒绝）
- `/admin/audit` — 操作审计：按状态筛选 + 翻页
- `/admin/connections` — 连接管理：增删改连接（含 SSH 跳板、key 路径），密码写入系统 keyring、配置只存 `keyring://` 引用，保存即热加载无需重启

> 连接管理页写入的密码进 keyring；SSH key 用路径填写（本地进程模式下为宿主真实路径），保存时校验文件存在且权限不过宽（需 chmod 600）。用页面管理连接后，配置文件由 UI 维护（会丢失手写注释）。

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
