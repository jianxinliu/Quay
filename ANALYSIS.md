# 分析工作台（Analysis Workbench）

> 把多个数据库、多张表、甚至本地文件的数据，拉进一块**本地分析沙箱**里自由地
> JOIN / 过滤 / 聚合，配上可视化，沉淀成可重跑的 workflow——
> 一个内嵌在 db-manage-mcp 里的迷你「跨源数据分析平台」，
> 而且**人和 AI Agent 都能用**。

## 它解决什么问题

日常数据分析的三个经典困境：

1. **数据散在不同地方**——prod MySQL 一份、备份 PG 一份、运营发来一个 CSV。
   想把它们 JOIN 到一起？导来导去，Excel 拼接，苦不堪言。
2. **生产库不敢重查询**——一条大 JOIN + GROUP BY 打到生产库，DBA 找上门。
3. **分析过程无法沉淀**——上周的分析 SQL 散落在聊天记录里，这周数据更新了，全部重来。

对 AI Agent 还有第四个：**agent 无法跨源分析**。agent 通过 MCP 只能对单个连接发
只读查询，结果集还要塞进模型上下文（大了放不下、算聚合还会错）。跨两个库的
JOIN，对 agent 来说不是慢，是**做不到**。

## 设计：不用 Spark，用 DuckDB

核心选型：**DuckDB**——进程内 OLAP 引擎（`pip install duckdb` 即用，零部署），
列式执行，单机千万行毫无压力。它把"类 Spark 虚拟表"的全部需求以 1% 的复杂度实现：

| 需求 | 实现 |
|---|---|
| 虚拟表 | DuckDB 库内的 TABLE（物化快照）与 VIEW（虚拟视图） |
| 多数据库聚合 | 经本服务的 reader 拉**快照**进工作区（带行数上限，受审计） |
| 文件数据 | DuckDB 原生 `read_csv_auto()` / Parquet / JSON |
| filter / join / group by | 完整 SQL（窗口函数、CTE 都有） |

```
源数据（现有连接管理：MySQL/PG/SQLite + SSH 多跳隧道 │ 本地 CSV/Parquet 文件）
   │   取数走现有 reader + 审计 + 行数上限（不打挂源库）
   ▼
分析工作区 = 一个独立的 .duckdb 文件（data/analysis/<名字>.duckdb）
   │   数据集 = 表快照；分析步骤 = SQL → 虚拟表（VIEW）
   ▼
查询台整套 UI 复用：Monaco 编辑器 / 上下文补全 / 结果表格 / 分页 / 导出
   ▼
可视化（P3）：图表视图（柱/线/饼/散点）
   ▼
Workflow（P2）：数据源 + 步骤 SQL 链 + 图表配置 → JSON 存档，一键重跑
```

## 安全模型：沙箱自由，源头受控

这是整个设计里最重要的一条边界：

- **工作区内**是本地"草稿纸"：CREATE / DROP / JOIN / UPDATE 随便玩，
  **不需要审批**——它不碰任何生产数据，坏了删掉重来。
- **从源库取数**永远走既有安全链路：reader 只读账号 + 全量审计 +
  快照行数上限。生产库红线一寸不退。

这给了使用者（尤其是 Agent）一个**可以自由写的安全沙箱**，同时保持对
生产数据"只读、留痕、限额"的管控。

## 对 AI Agent 开放（MCP 工具）

分析能力通过 MCP 工具暴露给 agent，把 agent 的数据分析范式从
"把数据搬进模型上下文里算"升级为"**计算下推给引擎，模型只看最终结果**"：

| MCP 工具 | 作用 |
|---|---|
| `analysis_workspaces` | 列出工作区与其中的数据集 |
| `analysis_import` | 从某连接把表/查询结果快照进工作区（limit 限额，审计） |
| `analysis_sql` | 在工作区内执行任意 SQL（含建虚拟表），返回结果 |

效益（相对现有 `query` 工具）：

- **跨源 JOIN 从不可能变为可能**（两个库、库 × CSV）
- 千万行 GROUP BY：引擎算，agent 只消费 20 行结果——**上下文成本 O(1)**
- 多步分析的中间结果留在虚拟表里，不再反复过模型上下文
- 模型不再"心算"聚合数字——SQL 引擎不会算错

最有想象力的闭环：**人在界面上沉淀 workflow，agent 按需重跑并解读结果；
agent 探索出的分析被保存为 workflow 给人复用**——知识在人与 AI 之间双向流动。

## 路线图

- [x] **P1 内核**：DuckDB 工作区、从连接导入快照（右键表 → 导入）、文件导入、
  查询台里工作区作为连接直接使用（树/编辑器/结果全复用）
- [x] **P1.5 Agent 开放**：`analysis_workspaces` / `analysis_import` / `analysis_sql`
  三个 MCP 工具（快照限额 + 审计）
- [x] **P2 Workflow**：取数配方（provenance 自动记录）+ 多语句脚本，保存 / 一键重跑
  （重拉源数据 → 顺序执行），失败标注在哪步；MCP `run_workflow` 开放给 agent
- [x] **P3 可视化**：结果区「表格/图表」切换（ECharts vendored），柱/线/饼/散点 +
  X/Y/聚合（SUM/COUNT/AVG/MIN/MAX）配置，配置随 workflow 保存、载入/重跑自动恢复图表视图
- [x] **P4 DAG 画布**：查询台「＋流程」打开可视化编排——取数/文件/过滤/JOIN/聚合/SQL/输出
  七类节点，拖拽连线成有向无环图；服务端把图编译为 `CREATE OR REPLACE VIEW`（拓扑序，
  环/断线/命名冲突有明确报错），中间节点即工作区视图可单独预览；运行逐节点标 ✓/✗；
  图随 workflow 保存（graph 列），载入/重跑原样恢复画布；agent 的 `run_workflow` 同样能跑
- [ ] P5（可选）：联邦直查（DuckDB ATTACH 远端 MySQL/PG，不落地）、定时运行

## 使用

**界面**：查询台左树右键任意表 → 「导入到分析工作区」；连接下拉选择工作区后，
树里就是你的数据集，编辑器里随便 JOIN。

**Agent**：直接对 MCP 说"把 A 库的订单表和 B 库的用户表各取 10 万行，
分析各城市客单价"——agent 会自己 import、JOIN、GROUP BY，只把结论带回对话。
