# Changelog

本项目的所有重要变更都记录在此。格式参考 [Keep a Changelog](https://keepachangelog.com/)，
版本遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Added
- **判定文案按读者选语言**：风险判定理由、审批单错误、「表不存在」、体检报告、流程/分析错误这类与 agent 共享的文案，agent 通过 MCP 看到的恒为英文，管理后台按系统设置「判定文案语言」（`text_language`，默认中文）显示；界面本身仍是中文。agent 创建的审批单落库时按后台语言存风险报告，审批页不会夹英文。

### Changed
- 文档：`/mcp` 端点不做 Host 校验作为设计边界写进 SECURITY.md「不在威胁模型内」（本机进程模式下 MCP 端点只服务本机 agent，接入 `DBM_ADMIN_ALLOWED_HOSTS` 之外的网络不是设计用法），不再列为 Known Issue。

## [0.1.0] - 2026-09-26

首个发布版本。以下是相对 main 上最近一次里程碑的变更。

### Added
- **零文件首跑**：`uvx --from "db-manage-mcp[keyring]" quay serve`（或 pipx）装完直接起；配置不存在时从包内模板生成（只含一条示例 SQLite 连接，其余引擎写法作注释），登录 token 首跑生成并存进 `~/.config/db-manage-mcp/env`；源码目录里跑仍用 `config/` 与 `data/`。新增 `quay` 命令别名。
- **随包播种的示例库**：首次启动在 `data/demo/shop.sqlite3` 生成 customers / orders 两张表，示例流程「渠道ROI分析」与示例配置的 `demo/shop` 连接都指向它，新装实例上点 ▶ 就能跑通。升级的实例：示例库与成本 CSV 会补齐，但已存在的旧示例流程仍指向 `local/demo-mysql`——删掉它（且没有别的流程）后重启会按新模板重新播种，或在画布上把取数节点改成 `demo/shop`。
- **看板首屏引导**：`/admin` 落在看板；一条连接都没有时显示三步引导；从未触达过的连接显示「未探测」而不是「正常」（新增健康位 `last_ok_at`，看板数据多一个 `unprobed` 计数）。
- **通知里的一次性审批链接**（默认关，设置 `notify_action_links`）：Bark / 企微 / 飞书通知可附一个点开即批准/拒绝的链接，不用登录后台；令牌 sha256 入库、用一次即作废、随审批单过期、后台先决策则失效；`GET /admin/approvals/{id}/act` 只展示、`POST` 才决策。
- `clickhouse` 安装 extra：ClickHouse 方言不再是硬依赖，缺失时建连给出安装提示。
- 查询台：存档里引用了已删除连接的 tab，没有未保存内容的自动关掉，其余保留并在组头标「已删除」。

### Changed
- **后台内容页跟随主题**：看板 / 审批 / 审计 / 设置与查询台共用一套主题，默认深色（主题设置改为全站生效）；内容页样式改走语义 token，看板图表颜色随主题；「被挡下」系列改为蓝色（在深色底上与红色的色弱区分度不够）。
- 启动信息改为 Quay 自己的一段（版本、后台/MCP 地址、配置与数据目录、token 来源），不再打印 FastMCP 横幅，也不再跑它的 PyPI 版本自检。
- `admin.py` 拆成 `dbmcp/admin/` 包（按页面/接口分模块 + `AdminContext`），纯搬运、路由表不变。
- 给 agent 看的文本（MCP instructions、28 个工具描述与参数说明、使用指南、结果元信息与错误提示）改为英文；面向后台操作者的中文界面不变。
- `CLAUDE.md` 只保留规则、红线与模块地图；经验教训迁到 `docs/LESSONS.md`，功能日志迁到 `docs/HISTORY.md`；`AGENTS.md` 改为指向 `CLAUDE.md` 的符号链接。

### Fixed
- 查询台：双击表名（以及右键「打开表数据」/「查看 DDL」、⌘P 表名搜索、⌘+点击表名）时，
  同一连接下的同一张表已经开着就切过去，不再重复开 tab；切换时若目标 tab 所在的连接分组是
  折叠的会自动展开，避免「切过去了但看不见」。

### Added
- **数据库体检（`db_checkup` MCP 工具 + 查询台「体检」按钮）**：一次调用拿到结构化诊断报告，
  agent 不必再为「这台 DB 健康吗」多轮 SQL 摸底。指标按六个维度组织（可用性 / 容量与连接 /
  查询性能 / 锁与并发 / 复制与高可用 / 存储与维护），逐项容错：
  - **MySQL（16 项）**：服务器信息、连接占用（含被 `max_connections` 拒绝的次数）、活跃线程、
    InnoDB 缓冲池命中率与压力（wait_free）、缓冲池 vs 数据量、慢查询、全表扫描 JOIN、
    临时表落盘比例、异常断连、死锁、长查询、行锁等待、无主键表、大事务落盘（binlog_cache）、
    复制延迟、大表 TOP5
  - **PostgreSQL（16 项）**：服务器信息、连接占用、空闲事务、长查询、等待事件、缓存命中率、
    临时文件落盘、死锁、死元组膨胀、统计信息过期、未使用索引、复制延迟、复制槽健康度
    （`wal_status`）、WAL 归档失败、事务 ID 回卷风险、库大小与大表 TOP5
  - **ClickHouse（8 项）**：磁盘健康（`is_broken` / 只读 / 剩余空间）、核心指标、失败查询、
    副本同步队列（会话过期 / log_pointer 落后）、活跃 part 数、未完成 mutation、大表 TOP5
  - **SQLite（5 项）**：完整性检查、空闲页碎片、日志模式、表行数
  每项给 status（ok / info / warn / critical / unknown）、人可读的 value 与解读建议，
  `overall` 取最严重项。**整库不可达时探活先行**：先跑一条 `SELECT 1`，连不上就直接给一条
  critical 的「数据库不可达：<真实原因>」（如 `Connection refused`），不再让十几项各自刷一遍
  `OperationalError` 噪音；库恢复后点「重新体检」即测全部指标。**`unknown` 是「没测到」而非「正常」**——视图选型已尽量避开权限门槛
  （见下条），真正缺权限的项会在报告 `privileges` 里汇总成**可复制的 GRANT 语句**
  （如 `GRANT pg_monitor TO probe_reader;`），缺权限项从「大面积 unknown」降到个别项。
  管理后台查询台连接栏新增「体检」按钮，走同一套逻辑（`GET /admin/sql/checkup`）；
  报告浮层按维度分组、组内按严重度排序，顶部摘要带状态计数，缺权限时顶部横幅列出影响项与 GRANT，
  底部支持「复制报告」（导出 Markdown）与「重新体检」，Esc / 点遮罩关闭。
- **体检的权限模型重设计**：让受限只读账号也能测出大部分指标，而不是一片 unknown。
  - PG：连接占用改用 `pg_stat_activity` 的行级可见性（`count(*)`，无需 `pg_monitor`），
    复制槽用官方 `wal_status` 枚举做确定性判定，统计类视图（`pg_stat_database` /
    `pg_stat_user_tables` / `pg_stat_user_indexes` / `pg_stat_archiver`）对只读账号无限制；
    只有需要 `state` / `query` / `wait_event` 列的项（空闲事务、长查询、等待事件、复制延迟）
    才在缺 `pg_monitor` 时标 unknown 并给出 GRANT 建议。
  - MySQL：长查询与锁等待改走 `performance_schema.threads` / `data_lock_waits`，
    不再依赖 `PROCESS` 权限（该视图对任何 SELECT 用户显示全部线程）；
    `SHOW GLOBAL STATUS` 本就零权限。
  - 累计计数器（死锁、临时文件、归档失败等）先按 uptime 折算成速率再判阈值，避免「累计值看起来大」的误报。
- **MCP 工具 `sync_table`：跨连接表同步（典型场景 线上库 → 本地库）**。可同步表结构
  （同引擎用源库建表语句原文；跨引擎用 sqlglot 转写成近似 DDL，被剥掉的二级索引/自增/字符集
  在返回值 `warnings` 里列明）与数据（按 `where` / `order_by` / `limit` 取一小撮，参数化批量写入）。
  `ddl` = skip / create_if_missing / recreate，`data` = none / append / replace 正交组合，
  `dry_run=True` 可只看计划。**数据量有硬上限**（默认 1000 行，系统设置 `sync_max_rows` 默认 10000）
  ——它是拉样本数据用的，不是全量迁移工具。
  写入走审批流：新增 `kind=sync` 的审批单（审批页展示人可读的同步计划），批准后按计划**重新取数**
  执行；服务端等待 / elicitation / `wait_for_change` / 后台「批准并立即执行」与 `execute` 完全一致。
  守卫：目标不能是 prod 连接、目标须配 writer 账号、ClickHouse 只能做源、Redis 不参与。
  审计 tool 为 `sync_read`（源库，reader）与 `sync_write`（目标库，writer）。
- 系统设置 DB tab 新增「表同步单次行数上限」（`sync_max_rows`）。
- README / README.en 补多客户端 MCP 接入指南（Claude Code、Codex、Cursor、DeepSeek Harness，
  以及 Claude Desktop / VS Code Copilot / Gemini CLI / Windsurf / 通用 stdio），并同步近期能力
  （ClickHouse、审批等待、begin_session / wait_for_change / export_table、通知渠道）。
- 开源治理文件：`LICENSE`（Apache-2.0）、`NOTICE`、`SECURITY.md`、`CONTRIBUTING.md`、本 `CHANGELOG.md`。
- `pyproject.toml` 补齐发布元数据（SPDX license、classifiers、keywords、project URLs、authors）。
- 管理后台本机来源校验：`Host` 白名单（`DBM_ADMIN_ALLOWED_HOSTS` 可扩展）+ 写请求 `Origin` 同源校验。

### Security
- **C2（CSRF / DNS rebinding）**：管理后台校验 `Host`/`Origin`，非本机来源一律 403，防恶意网页经
  DNS rebinding 触达 `127.0.0.1` 后台。
- **C3（生产写二次闸门）**：查询台对 prod 环境写操作，除风险确认外须再次输入连接名匹配才放行
  （对齐 Redis 控制台）。
- **H1（确认指纹绑定）**：写操作确认时绑定被评估 SQL 的指纹，确认前后 SQL 不一致即拒，防「看 A 批 B」。

## 里程碑（0.1.0 之前的开发历史）

早期迭代未打 tag，主要里程碑（详见 `CLAUDE.md` 的「当前状态」）：

- **M1–M4**：daemon 骨架 + SecretProvider；SSH 多跳隧道 + 引擎池；风险审计 + 拒绝—重提审批流
  （change_id 放行、writer 双账号）+ 管理后台；elicitation 快捷审批 + Redis 适配 + 脱敏 + CLI 审批。
- **M6 查询台**：DataGrip 风深色 SQL IDE（Vue 3 + Monaco，无构建）——库→表→列树、多 tab、
  上下文补全、光标处执行、分页、单元格就地编辑、图表、EXPLAIN 计划树、导出、片段库。
- **分析工作台**：DuckDB 本地沙箱跨源分析 + 可视化 DAG 编排 workflow，人和 agent 都能一键重跑。
- **Redis 控制台**：对标 Medis 单开一页（库→键前缀树、类型徽章、命令窗口、命令文档面板）。
- **Agent 侧**：MCP 工具（query/execute/analysis/workflow）；输出改紧凑 TSV + 结果大小双重硬限。

[Unreleased]: https://github.com/jianxinliu/Quay/compare/v0.1.0...main
[0.1.0]: https://github.com/jianxinliu/Quay/releases/tag/v0.1.0
