# Agent 使用指南

面向两类读者：**接入本服务的 AI agent**（本文可直接放进系统提示或知识库），以及
**给 agent 写提示词/做集成的人**。讲清楚 agent 有哪些工具、怎么组合、边界在哪。

服务地址：本地 HTTP `http://127.0.0.1:8100/mcp`（streamable HTTP），或 stdio
（`uv run dbm serve --stdio`）。工具的权威描述以 MCP 工具 schema 为准，本文讲的是**用法与套路**。

## 工具地图

| 场景 | 工具 | 说明 |
|---|---|---|
| 发现 | `list_projects` / `list_connections` | 找到目标连接（项目 → 连接） |
| 探索 schema | `list_tables` / `describe_table` / `sample_rows` | 表清单 / 列与索引 / 抽样看数据形状 |
| 只读查询 | `query(project, connection, sql)` | 仅 SELECT/SHOW/DESCRIBE/EXPLAIN；默认注入 LIMIT 与超时 |
| 数据变更 | `execute(project, connection, sql, reason?, change_id?)` + `get_change_status(change_id)` | 拒绝—重提审批流，见下 |
| 连通性 | `test_connection(project, connection)` | SELECT 1 |
| Redis | `redis_command(project, connection, command, reason?, change_id?)` | 读命令直通；危险命令走同一套审批 |
| 跨源分析 | `analysis_workspaces` / `analysis_import` / `analysis_sql` | DuckDB 本地沙箱，见下 |
| 流程沉淀 | `save_workflow(name, workspace, script)` | 把验证过的分析脚本存为可重跑 workflow |
| 流程重跑 | `run_workflow(name)` | 一键重跑人/agent 沉淀的分析流程 |

连接管理、密钥管理**不暴露给 agent**——那是人的事（CLI 或管理后台）。

## 基本套路

### 1. 查数（最常见）

```
list_projects → list_connections(project) → list_tables → describe_table → query
```

- `query` 只收只读语句，解析失败/多语句一律拒绝（默认拒绝原则）。
- 结果默认截断到连接配置的 max_rows（一般 1000）；要看全量统计请写聚合 SQL，
  不要翻页拉全表。
- 未绑定 database 的连接（工具返回会提示）需用**全限定表名**（`库.表`）。

### 2. 改数（拒绝—重提审批流）

```
execute(sql) → 返回 rejected + change_id + 风险报告
  → 在会话里告知用户，等人在管理后台批准
  → get_change_status(change_id) 查状态
  → 批准后：execute(sql, change_id=...) 重提，放行执行
```

要点：
- **第一次 execute 一定会被拒**，这不是错误，是流程。拿到 change_id 后把风险报告
  转述给用户，让用户去后台（或 CLI `dbm approve`）批。
- 重提必须是**同一条 SQL**（指纹校验，不一致直接拒）；真正执行的永远是审批单里存的 SQL。
- 审批单 30 分钟过期、一次性核销。prod 环境强制走审批，没有捷径。
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
规则：同名**脚本式** workflow 覆盖更新（迭代你自己的分析）；同名**画布 DAG**（人画的）
拒绝覆盖——换名或请用户在后台改。删除只能由人操作。

## 边界与行为准则

- 每条 SQL 都有审计留痕（你的 clientInfo、SQL 原文、结果、耗时）——行为可追溯。
- 敏感字段可能被脱敏（列值显示为掩码），这是配置行为，不要试图绕过。
- 拿不到权限/被审批卡住时：告知用户去管理后台处理，不要反复重试同一条写操作。
- 大表探索先 `describe_table` + `sample_rows`，别上来就 `SELECT *`。
- Redis：KEYS/FLUSHDB/FLUSHALL/CONFIG 等命令会进审批，读命令（GET/HGETALL/SCAN…）直通。
