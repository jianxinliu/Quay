# Changelog

本项目的所有重要变更都记录在此。格式参考 [Keep a Changelog](https://keepachangelog.com/)，
版本遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Fixed
- 查询台：双击表名（以及右键「打开表数据」/「查看 DDL」、⌘P 表名搜索、⌘+点击表名）时，
  同一连接下的同一张表已经开着就切过去，不再重复开 tab；切换时若目标 tab 所在的连接分组是
  折叠的会自动展开，避免「切过去了但看不见」。

### Added
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

### Known Issues
- `/mcp` 端点与后台同属 DNS rebinding 攻击面（读工具可被调用外传数据；写仍受审批挡）。修法为 ASGI
  Host 校验中间件，待单独排期。

## 里程碑（0.1.0 之前的开发历史）

早期迭代未打 tag，主要里程碑（详见 `CLAUDE.md` 的「当前状态」）：

- **M1–M4**：daemon 骨架 + SecretProvider；SSH 多跳隧道 + 引擎池；风险审计 + 拒绝—重提审批流
  （change_id 放行、writer 双账号）+ 管理后台；elicitation 快捷审批 + Redis 适配 + 脱敏 + CLI 审批。
- **M6 查询台**：DataGrip 风深色 SQL IDE（Vue 3 + Monaco，无构建）——库→表→列树、多 tab、
  上下文补全、光标处执行、分页、单元格就地编辑、图表、EXPLAIN 计划树、导出、片段库。
- **分析工作台**：DuckDB 本地沙箱跨源分析 + 可视化 DAG 编排 workflow，人和 agent 都能一键重跑。
- **Redis 控制台**：对标 Medis 单开一页（库→键前缀树、类型徽章、命令窗口、命令文档面板）。
- **Agent 侧**：MCP 工具（query/execute/analysis/workflow）；输出改紧凑 TSV + 结果大小双重硬限。

[Unreleased]: https://github.com/jianxinliu/Quay/commits/main
