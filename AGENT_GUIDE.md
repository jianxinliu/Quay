# Agent 使用指南

面向两类读者：**接入本服务的 AI agent**（本文可直接放进系统提示或知识库），以及
**给 agent 写提示词/做集成的人**。讲清楚 agent 有哪些工具、怎么组合、边界在哪。

把客户端接到本服务的步骤（Claude Code / Codex / Cursor / DeepSeek Harness / Claude Desktop /
VS Code Copilot / Gemini CLI / Windsurf / 通用 stdio）见 **[README.md 接入 Agent](README.md#接入-agent)**。
本文不重复粘贴各家配置，只写接到之后怎么用。

服务地址：本地 HTTP `http://127.0.0.1:8100/mcp`（streamable HTTP，**推荐**——与管理后台、
审批、审计共用一份常驻进程），或 stdio（`uv run dbm serve --stdio`）。本机已经有
`dbm serve` / launchd 在 8100 时，**不要再起一份 stdio**：那是另一个进程，审批单不会出现
在你正在看的后台里。工具的权威描述以 MCP 工具 schema 为准，本文讲的是**用法与套路**。

DeepSeek Harness 会把工具注册成 `mcp__<serverName>__<原名>`（例如 `mcp__dbm__query`）。
下面写的都是 MCP 原名；带前缀的客户端把前缀去掉即可对上。

客户端工具超时请 ≥ 180 秒（服务端默认等审批 120 秒）。超时不是失败：返回
`approval_required` 后调 `wait_for_change` 续等，审批单 60 分钟内有效。

## 工具地图

| 场景 | 工具 | 说明 |
|---|---|---|
| 声明会话 | `begin_session(title, note?)` | 开始跑 SQL 前调一次，登记本次会话的名字/背景，之后本会话的 SQL 在后台按会话归类、便于人回溯 |
| 发现 | `list_projects` / `list_connections` | 找到目标连接（项目 → 连接） |
| 探索 schema | `list_databases` / `list_tables` / `describe_table` / `sample_rows` | 库 / 表 / 列与索引 / 抽样看数据形状 |
| 只读查询 | `query(project, connection, sql)` | 仅 SELECT/SHOW/DESCRIBE/EXPLAIN；默认注入 LIMIT 与超时 |
| 数据导出 | `export_table(project, connection, table, fields?, limit?, format?, database?)` | 按库、表、字段和行数导出 CSV/JSON/Markdown/XLSX 文件 |
| 数据变更 | `execute(project, connection, sql, reason?, change_id?, wait_seconds?)` + `wait_for_change(change_id)` / `get_change_status(change_id)` | 提交后就地等人批准、批准即自动执行，见下 |
| 连通性 | `test_connection(project, connection)` | SELECT 1 |
| 跨源分析 | `analysis_workspaces` / `analysis_import` / `analysis_sql` | DuckDB 本地沙箱，见下 |
| 流程沉淀 | `save_workflow(name, workspace, script)` | 把验证过的分析脚本存为可重跑 workflow |
| 流程重跑 | `run_workflow(name)` | 一键重跑人/agent 沉淀的分析流程 |

连接管理、密钥管理**不暴露给 agent**——那是人的事（CLI 或管理后台）。

## 基本套路

### 0. 声明会话（推荐，开头调一次）

```
begin_session(title="排查订单重复扣款", note="复现 issue #123，只读排查")
```

登记后，本次 MCP 会话里跑过的所有 SQL（`query`/`execute`/`sample_rows`…）都会在管理后台
「操作审计」里按这个会话归类，人可以按 **agent + 会话** 回溯「这个会话都做了哪些操作」，
并把「需审批的写 / 不需审批的读」分开看。幂等，可重复调用更新名字。不调用也能工作，
只是后台只能看到一串没有语义的会话 id。

### 1. 查数（最常见）

```
list_projects → list_connections(project) → list_tables → describe_table → query
```

需要文件时使用 `export_table`。工具只返回文件名、大小、过期时间和短期下载链接，文件正文
保存在服务端、不进入 agent 上下文。未绑定默认库或需要切换库时，先调用
`list_databases`，再把选定的 `database` 传给 `list_tables`、`describe_table` 和
`export_table`。导出文件使用 reader 账号并应用 agent 敏感字段脱敏，行数不能超过连接
策略的 `max_rows`。

- `query` 只收只读语句，解析失败/多语句一律拒绝（默认拒绝原则）。
- **`query`/`sample_rows` 返回紧凑 TSV 文本**（非 JSON，省 token）：顶部 `#` 元信息行
  （`shown=N truncated=bool reason=... elapsed_ms=...`）+ `# types:` 列类型；随后首行是列名、
  其余是数据行，制表符分隔，`\N`=NULL，大整数为字符串（超 2^53 精度安全）。
- 结果有**两级硬上限**：① 行数（连接 max_rows，一般 1000）；② 字符预算
  （`agent_max_result_chars`，默认 40000≈12k token）。元信息 `truncated=true` = 没给全——
  **别重复拉全量/翻页**，改写聚合 SQL、加 WHERE/LIMIT、或用分析工作台（`analysis_*`）下推计算。
- 未绑定 database 的连接（工具返回会提示）需用**全限定表名**（`库.表`）。

### 2. 改数（审批流：提交 → 等人批 → 自动执行）

```
execute(sql) → 生成审批单，服务端就地等待人工决策（默认 120s，wait_seconds 可覆盖）
  → 你把返回的 approval_url 贴给用户，让其点开审批页
  → 用户点「批准」→ 本次调用自动执行并返回 status=executed（无需用户回来说一句、你也不用重提）
  → 若等待超时返回 status=approval_required：提醒用户后调 wait_for_change(change_id) 继续等
```

要点：
- **第一次 execute 不会立刻执行**，这不是错误，是流程。把风险报告转述给用户，
  **同时把 approval_url 贴出来**（终端里可点），省掉用户自己翻后台。
- 等待返回 `status=executed` 即已落地；返回 `status=consumed`（wait_for_change 里）表示
  审批人在后台点了「批准并立即执行」，变更已生效、`exec_result` 里有行数，**别再重提**。
- 别自己写循环反复调 `get_change_status` 轮询——用 `wait_for_change`，它一直等到有结果。
- 手动重提时必须是**同一条 SQL**（指纹校验，不一致直接拒）；真正执行的永远是审批单里存的 SQL。
- 审批单 60 分钟过期、一次性核销。prod 环境强制走审批，没有捷径。
- 客户端支持 elicitation 时（local/dev 环境），批准动作可能直接弹到会话里。

### 3. 跨源分析（分析工作台）

单库单表能解决的**不要**用分析工作台，直接 `query`。当你需要：
跨连接 JOIN、超过 max_rows 的大结果集聚合、多步骤加工——用它：

```
analysis_import(workspace, dataset, project, connection, sql)   # 每个源各拉一份快照
analysis_import(workspace, dataset2, project2, connection2, sql2)
analysis_sql(workspace, "SELECT ... FROM dataset JOIN dataset2 ...")  # 沙箱内自由分析
```

- 工作区是本地 DuckDB 沙箱：**沙箱内任意 SQL（含建表/建 VIEW/DELETE）不需要审批**，
  它不碰任何生产库。放心建中间视图做多步分析。
- **取数才是受控点**：analysis_import 走 reader 账号 + 审计 + 行数上限
  （默认 20 万行，硬上限 50 万）。取数 SQL 建议带 WHERE/聚合，把计算下推到源库。
- 核心心法：**计算下推，上下文只带小结果**。把 20 万行拉进沙箱聚合成 10 行再看，
  而不是把 20 万行塞进对话。
- 同名数据集会被替换（重跑友好）；`analysis_workspaces` 看现有工作区与数据集。

### 4. Workflow（沉淀与重跑）

人（或你协助人）在管理后台把一套分析沉淀为 workflow 后，你可以一键重跑：

```
analysis_workspaces  → 返回 {workspaces, workflows: [{name, workspace, kind}]}
run_workflow(name)   → {steps: [每步 ✓/✗ 与行数], output: 最终结果集, ok}
```

- workflow 有两种形态，对你**透明一致**：`kind=script`（多语句 SQL 脚本）与
  `kind=graph`（人在管理后台画的可视化 DAG：取数/过滤/JOIN/聚合/SQL/输出节点）。
  运行都是：重拉源数据 → 按序执行 → 返回逐步状态。
- 典型协作：人画好「渠道ROI分析」流程图 → 用户对你说"跑一下渠道 ROI 并解读" →
  你 `run_workflow("渠道ROI分析")` → 拿 output 表格做解读。数据是重新从源库拉的，
  结论永远新鲜。
- 某步失败时 `ok=false`，steps 里标明哪步、什么错——如实转述，不要猜测结果。
- 内置示例 `示例 · 渠道ROI分析` 可用来自检链路。

**沉淀你自己的分析**：做完一轮有价值的跨源分析后，用 `save_workflow(name, workspace, script)`
把它存下来——脚本是多语句 DuckDB SQL（分号分隔，引用工作区数据集，最后一条 SELECT
是输出），各数据集的取数配方自动随存。存之前先用 `analysis_sql` 把脚本逐句验证通过。
脚本必须**自包含**：依赖的中间 VIEW 要在脚本里 `CREATE OR REPLACE VIEW` 建出来，
不要依赖工作区里恰好存在的视图（重跑只重拉数据源，不会替你重建别处的视图）。
规则：同名**脚本式** workflow 覆盖更新（迭代你自己的分析）；同名**画布 DAG**（人画的）
拒绝覆盖——换名或请用户在后台改。删除只能由人操作。

## 边界与行为准则

- 每条 SQL 都有审计留痕（你的 clientInfo、SQL 原文、结果、耗时）——行为可追溯。
- 敏感字段可能被脱敏（列值显示为掩码），这是配置行为，不要试图绕过。
- 拿不到权限/被审批卡住时：把 approval_url 给用户并用 `wait_for_change` 等着，
  不要反复重提同一条写操作，也不要循环调 `get_change_status` 空轮询。
- 大表探索先 `describe_table` + `sample_rows`，别上来就 `SELECT *`。
- Redis 不对 agent 开放：没有 Redis MCP 工具，`list_connections` 也不会返回 Redis 连接。
  Redis 只能由人在管理后台的 Redis 控制台操作，需要 Redis 数据时请让用户去后台处理。

## 错误怎么读

所有错误都带一个方括号分类前缀。**照分类决定下一步，别盲目重发同一条 SQL**——
服务端把驱动异常统一翻译过了，你不会看到裸的 traceback 或连接串。

| 分类 | 含义 | 你该做什么 |
| --- | --- | --- |
| `[sql_syntax_error]` | SQL 语法错。**发到 DB 前就被拦下**：解析器初筛失败后，还让目标库「只解析不执行」地复核过一遍，所以这个结论是确定的 | 改写 SQL。原样重发一定还是失败，也不会浪费一次人工审批 |
| `[table_not_found]` / `[column_not_found]` | 表/列不存在 | 先 `list_tables` / `describe_table` 核对名字（未绑默认库时用「库名.表名」） |
| `[permission_denied]` / `[readonly_violation]` | 只读账号不能写 | 数据变更改走 `execute` 的审批流 |
| `[query_timeout]` | 查询超时 | 用 WHERE 收窄、走索引，或改用聚合／分析工作台下推计算 |
| `[duplicate_key]` / `[constraint_violation]` | 唯一键或外键/非空约束冲突 | 检查待写数据，或改用 UPSERT 语义 |
| `[connection_unavailable]` | 连接暂时断开，后台正在自动退避重连 | 按提示的秒数**稍后重试**即可，连接恢复后会自动放行 |
| `[connection_exhausted]` | 连续重连失败（**仍在每 5 分钟自动重试**，不会永久放弃） | 告诉用户去管理后台看一眼；后台查询台的连接告警条上有「立即重连」 |
| `[db_error]` | 未归类的数据库错误（已脱敏） | 按错误正文调整，不要原样重试 |

## 写 SQL 的约定（务必遵守）

- **时区**：本服务**不固定数据库会话时区**，`@@session.time_zone` 继承各库设置（可能是
  UTC+8，也可能是 UTC 或其它）。凡用 `FROM_UNIXTIME` / `UNIX_TIMESTAMP` / `NOW` / `CURDATE`
  / `DATE` 等**依赖会话时区**的函数，先 `SELECT @@session.time_zone` 确认，**切勿重复叠加
  时区偏移**。典型坑：会话已是 UTC+8，SQL 却又手动 `+28800`，等于 +16h，按天分组会把傍晚
  的数据串到第二天、同一个 day 裂成两行、日期整体偏后。
- **epoch 秒列按天分组**：优先纯算术 `FLOOR((ts+偏移)/86400)` 得 `day_idx`，日期由 day_idx
  反推 `DATE_ADD('1970-01-01', INTERVAL day_idx DAY)`，绕开 FROM_UNIXTIME 的隐式时区；
  day_idx 唯一决定日期，不必再把日期放进 `GROUP BY`。
- **尽量走索引**：`WHERE` / `JOIN` / `ORDER BY` 的过滤列尽量命中索引；先用 `describe_table`
  看有哪些索引，不确定就用 `query` 跑 `EXPLAIN`（`type=ALL` 或新版 `access_type=table` 即
  全表扫描）。**别对索引列套函数或运算**（如 `DATE(ts)`、`FROM_UNIXTIME(ts)`、`ts+1` 放在
  `WHERE` 左侧）——会使索引失效；应把转换放到**常量侧**、用范围比较（如
  `ts >= <epoch起> AND ts < <epoch止>`，而不是 `DATE(FROM_UNIXTIME(ts)) = '2026-07-15'`）。
- **多语句批量**：`execute` 支持分号分隔的多语句（如 `ALTER` + 回填 `UPDATE` 的迁移），
  整批一次审批、按语句拆开在同一事务逐条执行。注意 MySQL 的 DDL 会隐式提交，
  DDL+DML 混合批次无法整体回滚。
