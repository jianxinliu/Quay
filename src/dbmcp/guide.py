"""给 agent 的使用说明与最佳实践（会话首次调用工具时随结果附上一份）。

**为什么不只靠 MCP instructions**：instructions 由客户端在建连时呈现，各家处理差异很大
（有的截断、有的折叠、有的干脆只在系统提示最外层放一次），实测 agent 读不到或读过就忘。
所以除了保留 instructions，这里再准备一份完整说明，由 `server._FirstCallGuide` 中间件
在**每个会话的第一次成功工具调用**时挂在结果里——那时 agent 正要用它，读进去的概率最高，
且一个会话只发一次，不会持续占上下文。agent 也可以随时调 `usage_guide()` 重读。

内容取向：**按场景给组合**，而不是罗列工具。滥用往往不是因为不知道有哪个工具，
而是不知道「这种情况该用哪套」——把大表全量拉进上下文、把该聚合的活儿放到模型里做、
改数据不留回滚线索，都是这样来的。
"""

from __future__ import annotations

USAGE_GUIDE = """\
# Quay 数据库服务 · 使用说明与最佳实践

（本说明每个会话只发一次；随时可调 `usage_guide()` 重读。）

## 0. 先做这一件事

`begin_session(title, note)` —— 声明这次会话叫什么、要干什么。之后你跑的每条 SQL 都会
在后台按这个会话归类，人能回溯，你自己也能用 `list_sessions` / `session_history` 找回来。
不调也能用，但后台只会看到一串没有语义的会话 id。

## 1. 四条硬规矩

1. **读写分家，绕不过去。** 只读查询走 `query`（仅接受 SELECT/SHOW/DESCRIBE/EXPLAIN，
   用只读账号）；任何数据变更走 `execute`（生成审批单、人批准后才用写账号执行）。
   把写语句塞进 `query` 只会被拒，不要试。
2. **大结果不进上下文。** 先在 SQL 里聚合/收窄；确实需要整份数据就落文件
   （`export_table`）或进本地沙箱（`analysis_*`），只把结论带回来。
   把几万行拉进上下文既慢又贵，而且多半没有帮助。
3. **上下文是有配额的。** 本会话累计返回量有上限；撞到就会被拒绝取数，那时你必须
   **停下来问用户**是否确认继续这些耗 token 的查询，用户同意后调 `allow_more_results`
   再放行。别指望靠反复重试绕过去 —— 与其被拦，不如一开始就只取需要的那点数据。
4. **改数据前先想回滚。** 你自己判断这次改动值不值得留回滚线索；值得就先用 `query`
   查出改动前的旧值，写进 `execute(..., rollback_note=...)`。审批人当场看得到，
   事后你也能用 `session_history` 取回来拼回滚 SQL。无关紧要的改动留空即可。

## 2. 场景 → 用哪套工具

### 探索：我不知道有什么
- 有哪些库和连接 → `list_projects` → `list_connections`
- 有哪些表 → `list_tables`（未绑定默认库的连接先 `list_databases`）
- 表结构 → `describe_table`（字段/类型/索引/主键，最省上下文）
- 索引怎么建的、有没有分区、字符集/默认值/注释原文 → `table_ddl`（可逗号分隔传多张表）
- 长什么样 → `sample_rows(limit=10)`。**别用 `SELECT *` 去"看看"一张大表。**

### 查询：我要拿数
- 普通查询 → `query`。大表**必须**带 WHERE 或 LIMIT。
- 统计/汇总 → 聚合写进 SQL（GROUP BY / SUM / COUNT），别把明细拉回来自己数。
- 不确定量级 → 先 `SELECT COUNT(*) ... WHERE ...` 探一下，再决定怎么取。
- 慢 → 用 `query` 跑 `EXPLAIN` 看是不是全表扫描（access_type=ALL/table），
  对照 `describe_table` 的索引调整 WHERE。

### 结果太大
按这个顺序选，别跳步：
1. **能聚合就聚合** —— 在 SQL 里把结论算出来。
2. **要整份数据但不需要"读懂"** → `export_table`（CSV/JSON/Markdown/XLSX）。
   它返回 `download_url`，**用程序下载到目标位置，绝不要把文件内容读进上下文**。
3. **要多步处理 / 跨源 JOIN** → 分析工作台（本地 DuckDB 沙箱）：
   `analysis_import` 把各个源的查询结果快照成工作区数据集 → `analysis_sql` 在工作区里
   自由 JOIN/聚合/建 VIEW（沙箱内不需审批），只把小结果带回上下文。
   **跨库 JOIN 的正确姿势就是这个**，不是把两边都拉回来自己拼。

### 修改数据
1. 先 `query` 查出会被改到的行的旧值（要回滚线索时）。
2. `execute(sql, reason=..., rollback_note="改前 id=1001 status=2；回滚 UPDATE ...")`。
3. 返回里有 `approval_url` —— **把它贴给用户点开审批**。用户一批准，本次调用就自动执行
   并返回 `status=executed`，不需要用户回来跟你说"我批了"。
4. 等待超时返回 `status=approval_required` 时，提醒用户后调 `wait_for_change(change_id)`
   继续等。**别自己循环调 `get_change_status` 轮询。**
5. 迁移类改动（如 ALTER + 回填 UPDATE）可以一次提交多条语句（分号分隔），
   整批一次审批、在同一事务里逐条执行。

### 把线上的东西弄到本地
- **只要表结构**（在本地照着线上重建一套空表）→ `sync_table_ddl`，一次可传多张表。
- **要一小撮真实数据**跑起来 → `sync_table(data="append", where=..., limit=...)`。
  它是**拉样本**的，不是迁移工具：行数与体积都有服务端硬上限，超了会如实告诉你截断了。
- **要整份数据做备份** → 用 `sync_table_ddl` 建结构 + `export_table` 把数据取成文件，
  再用程序下载。**不要**指望用 `sync_table` 搬全量。
- 目标不能是 prod 连接。目标是 local/dev 时不需要审批，staging 才走审批流程。

### 回顾与回滚
- 我以前干过什么 → `list_sessions`（可按日期/关键词/项目/连接筛；
  `writes_only=True, status="ok"` = 真正改成过数据的会话）
- 那次具体做了什么 → `session_history(session_id)`。默认只回精简列；
  要 SQL 原文和错误明细得显式 `fields="sql,detail"`。
- 要回滚 → 从 `session_history` 取回当时写的 `rollback_note`，据此拼回滚 SQL，
  **回滚照样走 `execute` 审批**。

### 沉淀
反复要跑的分析 → `save_workflow` 存成可重跑流程，之后 `run_workflow` 一键重跑
（自动重拉源数据 → 逐步执行 → 返回每步状态）。可用列表见 `analysis_workspaces`。

## 3. 写 SQL 的约定

- **时区**：本服务不固定数据库会话时区，`@@session.time_zone` 继承各库设置（可能是 UTC+8，
  也可能是 UTC）。凡用 `FROM_UNIXTIME` / `UNIX_TIMESTAMP` / `NOW` / `CURDATE` / `DATE`
  这类依赖会话时区的函数，先 `SELECT @@session.time_zone` 确认，**切勿重复叠加偏移**
  （会话已是 UTC+8 又手动 +28800 等于 +16h，按天分组会把傍晚数据串到第二天）。
- **epoch 秒按天分组**：用纯算术 `FLOOR((ts+偏移)/86400)` 得 day_idx，日期由它反推
  `DATE_ADD('1970-01-01', INTERVAL day_idx DAY)`，绕开隐式时区。
- **别让索引失效**：不要对索引列套函数或运算（`DATE(ts)`、`FROM_UNIXTIME(ts)`、`ts+1`
  放在 WHERE 左侧）；改成对常量侧做转换、用范围比较（`ts >= 起 AND ts < 止`）。
- **大表必须收窄**：WHERE 或 LIMIT，至少有一个。

## 4. 结果长什么样

`query` / `sample_rows` 返回**紧凑 TSV 文本**（不是 JSON，省 token）：
顶部 `#` 元信息行 + `# types:` 列类型，随后首行列名、其余为数据行，制表符分隔，
`\\N` 表示 NULL，大整数以字符串传输（避免精度丢失）。

结果有**两级硬上限**：行数（默认 1000）与字符预算（默认约 12k token）。
元信息里 `truncated=true` 就是没给全 —— **不要原样重发去"再拉一次全量"**，
改用 WHERE/LIMIT 收窄、改成聚合，或走分析工作台把计算下推。

此外还有**会话级配额**：本会话累计返回量接近上限时，结果末尾会多出一行
`# budget: ...` 提醒；超出后取数会被拒（`[result_budget_exceeded]`）。
届时**先问用户**是否继续，用户同意后调 `allow_more_results(reason="用户已确认：...")`
再放行一个额度。放行记录会显示在后台看板上——不要在没问过用户的情况下调它。

## 5. 报错怎么读

所有错误都带一个方括号分类前缀，照着它决定下一步，别盲目重发同一条 SQL：

| 前缀 | 含义与下一步 |
| --- | --- |
| `[sql_syntax_error]` | 语法错（已由目标库复核确认），必须改写 SQL |
| `[table_not_found]` / `[column_not_found]` | 用 `list_tables` / `describe_table` 核对名字 |
| `[permission_denied]` / `[readonly_violation]` | 只读账号不能写；数据变更走 `execute` 审批流 |
| `[query_timeout]` | 收窄范围，或改用聚合 / 分析工作台 |
| `[connection_unavailable]` | 连接暂时断开、后台正在自动重连，按提示秒数稍后重试 |
| `[connection_exhausted]` | 连续重连失败（仍在重试），请提醒用户去后台看一眼 |
| `[result_budget_exceeded]` | 本会话取回的数据太多；停下来问用户，同意后 `allow_more_results` |

## 6. 边界：这些事做不到，别绕

- **Redis 不对 agent 开放**，只能由人在管理后台操作。
- **连接与密钥管理不暴露成工具**，你改不了连接配置，也拿不到任何账号密码。
- **不能往 prod 连接同步数据**；prod 上的写操作一律强制审批。
- 所有操作都会被审计（谁、什么时候、哪条连接、什么 SQL、什么结果），这是设计如此。
"""
