# Quay

**本地数据库工作台 —— 一个入口，让人和 AI Agent 都能安全地用你的所有数据库。**

各路数据库连接在此"停靠"(Quay = 码头):MySQL / PostgreSQL / SQLite / Redis、
SSH 多层跳板直达内网库，统一的连接与账密管理、SQL 风险审计与人工授权、全量操作留痕。
上层是四个开箱即用的前端 —— **查询台 · Redis 控制台 · 分析工作台 · MCP(给 Agent 的入口)** ——
共享同一套连接、同一条安全治理主线。

> 品牌名 **Quay**。内部包名仍是 `dbmcp`、CLI 仍是 `dbm`(改名只作对外展示,不动技术标识)。
> MCP 只是众多入口之一 —— 这不是"一个 MCP server",而是一个带治理的数据库工作台。

## 四个前端,一条治理主线

```mermaid
flowchart TB
    DB[("你的数据库们<br/>MySQL · PostgreSQL · SQLite · Redis<br/>（可经 SSH 多跳跳板直达内网）")]
    DB --> GOV["治理主线<br/>连接/账密管理 · reader/writer 双账号<br/>SQL 风险审计 · 拒绝—重提审批<br/>全量操作审计 · prod 强管控 · 脱敏"]
    GOV --> T1["🗄️ 查询台<br/>SQL IDE<br/><b>人用</b>"]
    GOV --> T2["🧊 Redis 控制台<br/>对标 Medis<br/><b>人用</b>"]
    GOV --> T3["🔬 分析工作台<br/>DuckDB 跨源分析<br/><b>人 + Agent</b>"]
    GOV --> T4["🤖 MCP<br/>给 Agent 的入口<br/><b>Agent 用</b>"]
```

- 🗄️ **查询台**(`/admin/sql`):DataGrip 风深色 SQL IDE。库→表→列树、多 tab、上下文补全、
  光标处执行、分页、单元格就地编辑、图表、EXPLAIN 计划树、导出、片段库。
- 🧊 **Redis 控制台**(`/admin/redis`):对标 Medis。库→键前缀树、类型徽章、键详情、
  命令窗口(读直通/写确认)、命令文档面板。
- 🔬 **分析工作台**:把不同库/文件的数据快照进本地 DuckDB 沙箱自由 JOIN 分析,
  可视化 DAG 编排 workflow,人和 Agent 都能一键重跑。
- 🤖 **MCP**:给 Agent 的受控通道。只读直通、写操作走"拒绝—重提 + 人工授权",全程审计。

## 文档地图

| 你是谁 | 看哪份 |
|---|---|
| **使用后台的人** | [USER_GUIDE.md](USER_GUIDE.md) — 查询台 / Redis 控制台 / 分析工作台 / DAG 画布 / 审批操作手册 |
| **接入的 AI Agent**(或写 agent 提示词的人) | [AGENT_GUIDE.md](AGENT_GUIDE.md) — 工具地图、审批流套路、跨源分析与 workflow 用法 |
| **开发者** | [DESIGN.md](DESIGN.md) 架构与安全设计 · [ANALYSIS.md](ANALYSIS.md) 分析工作台设计 · CLAUDE.md 开发约定与经验 |

## 快速开始

```bash
uv sync --extra keyring
cp config/connections.example.yaml config/connections.yaml   # 按需修改

# 前台运行(开发/调试)
DBM_MYSQL_PW=... DBM_ADMIN_TOKEN=你的token uv run dbm serve
# stdio 模式(单 agent 直连)
uv run dbm serve --stdio
```

管理后台 `http://127.0.0.1:8100/admin/login`,MCP 端点 `http://127.0.0.1:8100/mcp`。

Claude Code 注册 MCP:

```bash
claude mcp add --transport http dbm http://127.0.0.1:8100/mcp
```

### 常驻服务(macOS launchd,开机自启 + 崩溃自动拉起)

```bash
bash scripts/install-launchd.sh          # 安装并启动,首次会生成管理 token
# 密钥写在 ~/.config/db-manage-mcp/env(600 权限),按需补 DBM_MYSQL_PW=...
bash scripts/install-launchd.sh          # 改配置/密钥后重跑即热重启(幂等)
bash scripts/install-launchd.sh --uninstall
tail -f ~/Library/Logs/db-manage-mcp.log
```

### 双击启动(生成 macOS .app)

不想记命令行的话,生成一个可双击的 **Quay.app**:双击即确保服务已起(装了 launchd 就拉起、
没装就直接后台启动),等端口就绪后用默认浏览器打开管理后台。

```bash
bash scripts/build-app.sh                 # 生成 ./Quay.app
bash scripts/build-app.sh ~/Applications  # 放进启动台/Spotlight(推荐)
```

本地构建无 quarantine 标记,双击不触发 Gatekeeper(无需签名/公证)。图标已内置
(`scripts/app-icon.png`,想换自己的换掉重跑即可)。**仓库整体搬家后需重跑**
(PROJECT_DIR 在构建时写死)。

## MCP 工具(给 Agent)

| 工具 | 说明 |
|---|---|
| `list_projects` / `list_connections` | 浏览可用连接(不含账号密码;Redis 有意不出现) |
| `query(project, connection, sql)` | 只读 SQL;非只读语句一律拒绝并审计;缺 LIMIT 自动注入兜底 |
| `execute(project, connection, sql, reason?, change_id?)` | 写操作:首次提交生成审批单返回 change_id;批准后带 change_id 重提执行 |
| `get_change_status(change_id)` | 审批单状态与风险报告 |
| `list_tables` / `describe_table` / `sample_rows` | schema 探索 |
| `test_connection` | 连通性检查 |
| `analysis_workspaces` | 列出分析工作区、数据集及已保存 workflow |
| `analysis_import(workspace, dataset, project, connection, sql, limit?, schema?)` | 从某连接把查询结果快照进工作区(reader 拉取,带行数上限,受审计) |
| `analysis_sql(workspace, sql, max_rows?)` | 在工作区执行 SQL(DuckDB 方言,自由 JOIN/聚合,不需审批) |
| `save_workflow(name, workspace, script)` | 把分析脚本沉淀为可重跑 workflow(取数配方自动随存;不可覆盖人画的 DAG) |
| `run_workflow(name)` | 一键重跑已保存的分析 workflow(脚本式或 DAG 画布) |

> **Redis 有意不暴露给 Agent** —— 只有人能通过后台 Redis 控制台操作。

## 查询台(/admin/sql)

DataGrip 风格的深色 SQL IDE,日常"连库—看表—查数—导出"一屏完成:

- **对象树**:库 → tables(N) → 表 → columns/keys/indexes,表容量 M/G/T 分级展示;
  ⌘ 多选表 → 右键批量 DROP(红色确认条,不可逆操作二次确认)
- **编辑器**:Monaco(VS Code 内核),上下文感知补全(FROM 后补表、SELECT/WHERE
  位置补当前语句各表的列、`库.` 补表、`别名.` 补列)、多语句光标处执行、
  sqlglot 格式化、EXPLAIN 可视化计划树
- **表数据视图**:双击表打开,WHERE / ORDER BY 输入条走 SQL 重查(跨页正确),
  点列头排序,双击单元格就地编辑、行级增删克隆(生成 INSERT/DELETE 走写确认流)、
  CSV/粘贴导入(Excel 区域直接粘,参数化单事务)、选中行复制为 TSV/INSERT/Markdown、
  ⌘F 网格内搜索、⌘P 跨库表名跳转、SQL 实时语法标红
- **结果可视化**:结果区一键切「表格 / 图表」(ECharts),柱/折线/饼/散点 +
  X/Y 列与聚合(SUM/COUNT/AVG/…)配置;图表配置随 workflow 保存,重跑自动出图
- **多 tab**:查询 / 表数据 / DDL 三种类型,可拖拽排序、可 pin;
  全部状态(含结果集)持久保活,关页面重开原样恢复;
  **查询在服务端异步执行,切页不中断**,回来自动续接结果
- **写安全**:写语句先弹风险报告(影响表/行数/索引/执行计划)确认后才由
  writer 账号执行,全程审计——这是后台旁路,Agent 的写操作仍必须走审批流

## Redis 控制台(/admin/redis)

对标 Medis 的独立页(键值模型和 SQL 差异大,不混在查询台里):

- **左栏**:库(列全部逻辑库、有数据的带键数,库数很大的实例带上限防刷屏)→
  键(`:` 前缀树 + STRING/HASH/LIST/SET/ZSET 彩色徽章;点文件夹深度优先展开一条链)
- **键详情**:类型感知展示 + TTL / 内存 / 编码;msgpack 值自动解码成 JSON
- **命令窗口**:Monaco,光标/选中行执行;读直通、写确认旁路;**prod 写命令须输入连接名确认**;
  `CONFIG GET`/`ACL` 结果里的密码/口令哈希自动脱敏
- **命令文档面板**:随光标命令切换,链 redis.io,覆盖 176 条常用命令

## 分析工作台

把不同库、不同表、本地 CSV/Parquet 的数据快照进本地 DuckDB 沙箱,
自由 JOIN / 聚合 / 建虚拟表,Agent 也能通过 MCP 使用——跨源分析对 Agent
从"不可能"变为"一句话"。

**可视化 DAG 编排**:查询台点「＋流程」,拖节点(取数/文件/过滤/JOIN/聚合/SQL/输出)
连线成数据流图,一键运行逐节点标注状态;图随 workflow 保存,人和 Agent 都能一键重跑。
不会写 SQL 也能搭跨源分析流程。详见 **[ANALYSIS.md](ANALYSIS.md)**。

## 写操作授权流程

1. agent 调 `execute` 提交写 SQL → 评估风险、生成审批单、**拒绝**并返回 `change_id`
2. 人在 `/admin/approvals` 查看风险报告后批准/拒绝(或 elicitation 会话内确认 / CLI)
3. 批准后 agent 带 `change_id` 重提 → 执行的是**审批单里存储的 SQL**(重提文本仅作指纹校验)
4. 被拒则返回人的理由,agent 调整后重新发起

三种审批通道:elicitation 会话内确认(local/dev 默认开)、管理后台、CLI
(`dbm approvals` / `dbm approve <id>` / `dbm reject <id>`)。无论哪种,审批单都留痕。

## 安全模型

- **默认拒绝**:sqlglot AST 分类,解析/分词失败、多语句、CTE 夹带 DML、
  `SELECT FOR UPDATE` 等一律按写操作处理
- **双账号**:日常查询走只读 reader;仅审批通过的执行切换 writer
- **数据库层第二道防线**:MySQL `SESSION TRANSACTION READ ONLY`、
  PG `default_transaction_read_only=on`、SQLite `PRAGMA query_only`
- **密钥不落明文**:配置只存 `env://` / `keyring://` 引用,密码不进日志与返回值;
  Redis `CONFIG`/`ACL` 结果中的凭证自动脱敏
- **全量审计**:每次调用(含被拒的)记 agent、时间、连接、SQL、行数、耗时
- **连接与密钥管理不暴露给 agent**:只有人能通过已登录后台或 CLI 操作;prod 强管控

## 资源与数据治理

- 引擎与 SSH 隧道空闲 10 分钟自动回收;断开的隧道按需重建
- 审计与终态审批单默认保留 30 天(`--retention-days` / `DBM_RETENTION_DAYS`)
- 缺 LIMIT 的 SELECT 自动注入 `LIMIT max_rows+1` 兜底,防大表拉挂 DB
- 大单元格截断(`policy.max_cell_chars`,默认 4096),防大 TEXT/BLOB 撑爆上下文
- 敏感字段脱敏:内置模式(password/token/secret…)+ `policy.mask_columns`,
  命中值替换为 `***MASKED***`
- SSH 多跳:`jump_hosts` 按序跳板;自定义密钥/known_hosts 用
  `ssh_options: ["-F", "/path/ssh_config"]`(`-o` 不会传递给跳板连接)

## 开发

```bash
uv run pytest        # 全量测试(改动后必须全过)
```

部署形态为**本地进程模式**(有意不用 Docker:本地单机场景下连宿主库要绕网络、
keyring 无后端、SSH key 变容器内路径,纯增麻烦)。
