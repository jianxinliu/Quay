# db-manage-mcp

**给所有 AI Agent 一条安全的数据库通道，给人一个开箱即用的数据工作台。**

一个本地常驻的 MCP 服务 + 管理后台：按项目管理数据库连接与账密、SSH 多层跳板、
SQL 风险审计与人工授权、全量操作审计；内置一个 **DataGrip 风格的 SQL 查询台**
和一个**跨源数据分析工作台**（人和 Agent 都能用）。

- 设计文档：[DESIGN.md](DESIGN.md) · 分析工作台：[ANALYSIS.md](ANALYSIS.md)

## 能做什么

**对 Agent（MCP）**
- 只读查询直通，写操作走「拒绝—重提 + 人工授权」流程，全程审计
- MySQL / PostgreSQL / SQLite / Redis，SSH 多跳隧道直达内网库
- 跨源数据分析沙箱：把多个库/文件的数据快照进 DuckDB 工作区自由 JOIN 分析

**对人（管理后台）**
- **查询台**：深色 IDE（Vue 3 + Monaco），库→表→列树（含表容量）、多 tab（可 pin）、
  上下文感知 SQL 补全、光标处执行、分页 + WHERE/ORDER BY 条、单元格就地编辑、
  EXPLAIN 计划树、查询历史、SQL 片段库、CSV/JSON/Markdown/Excel 导出、
  批量 DROP 二次确认——切页/刷新状态保活，查询在服务端继续跑
- **审批中心**：写操作风险报告（影响行数/索引命中/执行计划）+ 批准/拒绝
- **操作审计**：谁、何时、哪个库、什么 SQL、什么结果，筛选 + 翻页
- **连接管理**：弹窗表单、账号权限校验（拦截误配的高权限只读账号）、
  密码进系统 keyring、保存即热加载

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

Claude Code 注册：

```bash
claude mcp add --transport http dbm http://127.0.0.1:8100/mcp
```

### 常驻服务（macOS launchd，开机自启 + 崩溃自动拉起）

```bash
bash scripts/install-launchd.sh          # 安装并启动，首次会生成管理 token
# 密钥写在 ~/.config/db-manage-mcp/env（600 权限），按需补 DBM_MYSQL_PW=...
bash scripts/install-launchd.sh          # 改配置/密钥后重跑即热重启（幂等）
bash scripts/install-launchd.sh --uninstall
tail -f ~/Library/Logs/db-manage-mcp.log
```

## MCP 工具

| 工具 | 说明 |
|---|---|
| `list_projects` / `list_connections` | 浏览可用连接（不含账号密码） |
| `query(project, connection, sql)` | 只读 SQL；非只读语句一律拒绝并审计；缺 LIMIT 自动注入兜底 |
| `execute(project, connection, sql, reason?, change_id?)` | 写操作：首次提交生成审批单返回 change_id；批准后带 change_id 重提执行 |
| `get_change_status(change_id)` | 审批单状态与风险报告 |
| `redis_command(...)` | Redis：读命令直通；写命令走授权；FLUSHDB/KEYS/EVAL 按 CRITICAL 管控 |
| `list_tables` / `describe_table` / `sample_rows` | schema 探索 |
| `test_connection` | 连通性检查 |
| `analysis_workspaces` / `analysis_import` / `analysis_sql` | **分析沙箱**：跨源快照导入 + 自由 SQL 分析，见 [ANALYSIS.md](ANALYSIS.md) |

## 查询台（/admin/sql）

DataGrip 风格的深色 SQL IDE，日常"连库—看表—查数—导出"一屏完成：

- **对象树**：库 → tables(N) → 表 → columns/keys/indexes，表容量 M/G/T 分级展示；
  ⌘ 多选表 → 右键批量 DROP（红色确认条，不可逆操作二次确认）
- **编辑器**：Monaco（VS Code 内核），上下文感知补全（FROM 后补表、SELECT/WHERE
  位置补当前语句各表的列、`库.` 补表、`别名.` 补列）、多语句光标处执行、
  sqlglot 格式化、EXPLAIN 可视化计划树
- **表数据视图**：双击表打开，WHERE / ORDER BY 输入条走 SQL 重查（跨页正确），
  点列头排序，双击单元格就地编辑（按主键生成 UPDATE，走写确认流）
- **结果可视化**：结果区一键切「表格 / 图表」（ECharts），柱/折线/饼/散点 +
  X/Y 列与聚合（SUM/COUNT/AVG/…）配置；图表配置随 workflow 保存，重跑自动出图
- **多 tab**：查询 / 表数据 / DDL 三种类型，可拖拽排序、可 pin；
  全部状态（含结果集）持久保活，关页面重开原样恢复；
  **查询在服务端异步执行，切页不中断**，回来自动续接结果
- **写安全**：写语句先弹风险报告（影响表/行数/索引/执行计划）确认后才由
  writer 账号执行，全程审计——这是后台旁路，Agent 的写操作仍必须走审批流

## 分析工作台

把不同库、不同表、本地 CSV/Parquet 的数据快照进本地 DuckDB 沙箱，
自由 JOIN / 聚合 / 建虚拟表，Agent 也能通过 MCP 使用——跨源分析对 Agent
从"不可能"变为"一句话"。

**可视化 DAG 编排**：查询台点「＋流程」，拖节点（取数/文件/过滤/JOIN/聚合/SQL/输出）
连线成数据流图，一键运行逐节点标注状态；图随 workflow 保存，人和 Agent 都能一键重跑。
不会写 SQL 也能搭跨源分析流程。详见 **[ANALYSIS.md](ANALYSIS.md)**。

## 写操作授权流程

1. agent 调 `execute` 提交写 SQL → 评估风险、生成审批单、**拒绝**并返回 `change_id`
2. 人在 `/admin/approvals` 查看风险报告后批准/拒绝（或 elicitation 会话内确认 / CLI）
3. 批准后 agent 带 `change_id` 重提 → 执行的是**审批单里存储的 SQL**（重提文本仅作指纹校验）
4. 被拒则返回人的理由，agent 调整后重新发起

三种审批通道：elicitation 会话内确认（local/dev 默认开）、管理后台、CLI
（`dbm approvals` / `dbm approve <id>` / `dbm reject <id>`）。无论哪种，审批单都留痕。

## 安全模型

- **默认拒绝**：sqlglot AST 分类，解析/分词失败、多语句、CTE 夹带 DML、
  `SELECT FOR UPDATE` 等一律按写操作处理
- **双账号**：日常查询走只读 reader；仅审批通过的执行切换 writer
- **数据库层第二道防线**：MySQL `SESSION TRANSACTION READ ONLY`、
  PG `default_transaction_read_only=on`、SQLite `PRAGMA query_only`
- **密钥不落明文**：配置只存 `env://` / `keyring://` 引用，密码不进日志与返回值
- **全量审计**：每次调用（含被拒的）记 agent、时间、连接、SQL、行数、耗时
- **连接与密钥管理不暴露给 agent**：只有人能通过已登录后台或 CLI 操作

## 资源与数据治理

- 引擎与 SSH 隧道空闲 10 分钟自动回收；断开的隧道按需重建
- 审计与终态审批单默认保留 30 天（`--retention-days` / `DBM_RETENTION_DAYS`）
- 缺 LIMIT 的 SELECT 自动注入 `LIMIT max_rows+1` 兜底，防大表拉挂 DB
- 大单元格截断（`policy.max_cell_chars`，默认 4096），防大 TEXT/BLOB 撑爆上下文
- 敏感字段脱敏：内置模式（password/token/secret…）+ `policy.mask_columns`，
  命中值替换为 `***MASKED***`
- SSH 多跳：`jump_hosts` 按序跳板；自定义密钥/known_hosts 用
  `ssh_options: ["-F", "/path/ssh_config"]`（`-o` 不会传递给跳板连接）

## 开发

```bash
uv run pytest        # 全量测试（改动后必须全过）
```

部署形态为**本地进程模式**（有意不用 Docker：本地单机场景下连宿主库要绕网络、
keyring 无后端、SSH key 变容器内路径，纯增麻烦）。
