# Quay（包名 dbmcp / CLI dbm）

**品牌名 Quay（码头）**：本地数据库工作台——查询台 · Redis 控制台 · 分析工作台 · MCP（给 agent）四个前端共享一套连接与安全治理主线。改名只是对外品牌：**包名仍 `dbmcp`、CLI 仍 `dbm`、配置目录 `~/.config/db-manage-mcp`、launchd label `com.db-manage-mcp`、keyring service `db-manage-mcp` 全部不变**（只换品牌、不做全量重命名，避免动迁移逻辑）。按项目管理连接与账密、SSH 多层跳板、SQL 审计与人工授权、操作审计与管理后台。完整设计见 `DESIGN.md`（改动核心流程前先读它）。

## 技术栈（已确认，勿擅自更换）

- Python 3.12 + FastMCP（官方 MCP SDK）+ FastAPI（MCP 与管理后台共用一个 ASGI 应用）
- sqlglot 做 SQL AST 解析与审计；SQLAlchemy Core（不用 ORM）+ redis-py
- SQLite 存审计/审批单/元数据缓存；管理后台服务端渲染 HTML（未用 Jinja2/HTMX）
- SSH 多跳复用系统 OpenSSH（`ssh -J jump1,jump2`），不自己实现隧道协议
- 部署形态：**本地进程模式**（`uv run dbm serve`），macOS 用 launchd 常驻（scripts/install-launchd.sh）；stdio 作单 agent 直连模式。**已弃用 Docker**——本地单机场景 Docker 只带麻烦（连宿主库绕网络、无 keyring 后端、SSH key 变容器内路径）

## 安全设计红线（实现时必须遵守）

1. **默认拒绝**：SQL 解析失败、多语句、无法分类的一律按写操作处理，进审批流。
2. **密钥不落明文**：配置文件只存 `env://` / `keyring://` 引用；密码永不出现在日志、审计记录、工具返回值中。本地进程模式下管理后台可把页面输入的密码写 keyring；`env://` 引用的值由常驻服务从 `~/.config/db-manage-mcp/env`（600）注入。
3. **拒绝—重提 + change_id 放行**：写操作先被明确拒绝并生成审批单（含风险报告），人在后台批准后放行（agent 带 change_id 重提，或后台「批准并立即执行」当场核销执行）；**执行的永远是审批单里存储的 SQL**，重提文本只作指纹校验（不一致即拒），指纹匹配仅作无 id 时的兜底。审批单一次性核销、有 TTL（60 分钟）。首提后服务端就地等人决策（默认 120s），批准即自动执行；超时用 `wait_for_change` 续等。prod 环境强制审批。
4. **双账号**：日常查询走只读账号，仅审批通过的执行切换 writer 账号。
5. **连接与密钥管理不暴露为 MCP 工具**（agent 碰不到）；人可通过 CLI 或**已登录的管理后台**操作。后台需认证（DBM_ADMIN_TOKEN），页面输入的密码一律进 keyring、配置只存引用。
6. 通知遵循"安静即正常"：不主动推送，审批挂起由 agent 在会话中告知用户。

## 约定

- 每条 SQL 必须落审计记录：agent（clientInfo + session）、时间、连接、SQL 原文与指纹、风险等级、结果（行数/耗时/状态）、审批信息。
- 查询默认加 LIMIT（1000）与语句超时（30s），可按连接配置。
- Redis 走命令分类模型：读命令直通；KEYS / FLUSHDB / FLUSHALL / CONFIG 属 CRITICAL 需审批。

## 开发

```bash
uv sync --extra keyring --extra tokenizer --extra clickhouse   # 安装依赖（clickhouse 是可选方言）
uv run pytest              # 全量测试（改动后必须全过）
uv run dbm serve           # HTTP（127.0.0.1:8100）；配置/数据/登录 token 首跑自动生成（源码目录用 config/ data/，pipx/uvx 装的用 ~/.config/db-manage-mcp/）
uv run dbm serve --stdio                                # stdio 模式
bash scripts/install-launchd.sh                         # macOS 常驻（幂等，改配置后重跑即热重启）
```

- 代码在 `src/dbmcp/`，分层：`server.py`（MCP 接口）→ `service.py`（核心逻辑，与传输解耦、可直接单测）→ `engines.py` / `audit/`（分类器 + 审计日志）。新增工具时逻辑写在 service 层，server 层只做注册和 ToolError 转换。
- **起临时服务做测试：必须换端口 + 换数据目录，绝不用 8100**。`8100` 是 launchd 正式实例、也是 Claude MCP 客户端连接的端口；在 8100 上另起测试服务会抢端口打断正式 MCP，且共用 `data/dbm.sqlite3` 会污染正式的设置/审计/审批数据。测试实例一律：`uv run dbm serve --port 8201 --data-dir /tmp/dbm-test-data`（端口任选非 8100，数据目录指到临时目录）。测完清理临时目录。浏览器 e2e 也连测试端口。

## 开发注意（操作规则）

踩坑的完整记录在 `docs/LESSONS.md`；这里只留每次动手前必须记得的规则。

- **包名是 `dbmcp`**（`dbm` 是标准库模块名，会被遮蔽）；CLI 是 `dbm`，`quay` 是同一入口的别名。
- **本机测本地服务要绕代理**：`curl --noproxy '*'`；fastmcp Client 用 `env NO_PROXY='*' no_proxy='*'`；手工起测试实例前清掉代理变量 `env ALL_PROXY= all_proxy= HTTP_PROXY= HTTPS_PROXY= NO_PROXY='*'`。**反过来** AI 直连（claude/codex CLI、HTTP API）是远程调用，必须走代理，别 `NO_PROXY='*'` 一刀切。
- **DB 方言相关代码必须对目标 DB 跑真实 e2e**（`scripts/e2e_*.py`/`.sh`），SQLite 单测发现不了会话设置、超时、EXPLAIN 格式、DDL 语法这类差异。
- **写 sqlglot 逻辑前先跑实验脚本验证解析行为**；解析/分词失败 catch 基类 `SqlglotError`，默认拒绝按写处理；给 sqlglot 解析和给 DB 执行是两条路，归一化只作用于前者。
- **grep 大前端文件要加 `-a`**（`console.js` 等会被当成二进制静默返回空，据此下「代码被删了」的结论会错）。
- **改 .py 要重启测试服务**（uvicorn 不热重载）；自家静态 js/css 是 no-cache，刷新即生效。
- **浏览器 e2e**：合成打字对 Monaco 不可靠，用 `javascript_tool` 直接 `monaco.editor.getEditors()[0].getModel().setValue(...)`；Vue 生产构建拿不到 `app._instance`，要造状态就在**不加载查询台的页面**上改 localStorage（键 `dbm-console-v2`）再进查询台。
- **常驻服务里的辅助线程一律 daemon**；**离开进程边界的副作用**（通知/邮件/webhook）在库/服务层默认 no-op，只有 `_cmd_serve` 注入真实现——否则跑测试会真的发通知。
- **密钥不落日志、审计、工具返回值**：语句文本可能含密钥的功能（DCL、`CONFIG SET requirepass`）在生成处就分执行版与审计版。
- **批量用脚本改源码后 `grep` 确认每处都落盘了**（漏了 `write_text` 测试照样绿）。
- **知识落点**：新踩的坑写 `docs/LESSONS.md`（现象 → 根因 → 修法 → 回归测试）；功能完成写 `CHANGELOG.md`（用户可见）与 `docs/HISTORY.md`（实现细节）；本文件只放规则、红线、模块地图。

## 模块地图（src/dbmcp/）

- `ai.py` AI 辅助生成 SQL（`build_sql_prompt`/`build_followup_prompt` 纯函数拼 prompt + `run_ai` provider 分发 claude/codex CLI + `parse_ai_output` 解析，`generate_sql` 串起来；`AIResult` 带 session_id 支持续接会话）；service `ai_generate_sql`、admin `/admin/sql/ai`、console.js「✨ AI」浮层
- `server.py` MCP 工具注册 → `service.py` 核心逻辑（可单测）；`ConnectionUnavailable` 转带 `[connection_unavailable]`/`[connection_exhausted]` 前缀的 ToolError
- `health.py` 连接健康位断路器（`ok/unavailable/exhausted`）+ 后台重连（5→15→30→45→60s 退避，**封顶 60s 后持续重试、无终态放弃**）+ 退避到点的 half-open 放行（DB 恢复即自愈）+ `is_connection_error` 分类；service `_run_touching_db` 包每个 DB 触达入口，agent 侧看到明确"稍后重试/需人介入"；`GET /admin/sql/health` 给查询台画告警条
- `metrics.py` 运行指标：`LiveOps`（在途操作登记簿——审计只在操作**结束后**落库，「此刻谁在查」只有它答得了；由 `service._run_touching_db` 统一登记/注销）+ `estimate_result_bytes`（结果集体积估算，落进 `audit_log.result_bytes` 供看板统计流量）
- `budget.py` 会话级结果配额：`SessionBudget` 按会话累计 agent 取回的字符数，超额抛 `ResultBudgetExceeded`（文案是「去问用户」而非「换写法重试」）；`allow_more_results` 工具在用户点头后 `grant()` 追加一份额度，放行次数与理由在看板可见。只作用于 agent 的 `query`/`sample_rows`（server.py 的 `_budget_gate`），后台/人的路径不经过
- `guide.py` 给 agent 的完整使用说明（按场景给工具组合）：由 `server._FirstCallGuide` 中间件在**每个会话第一次成功工具调用**时作为额外文本块附在结果里（不动 structured_content），`usage_guide()` 工具可随时重读
- `errors.py` 驱动异常 → 分类化 + 已脱敏的错误（`translate_db_error`/`sanitize_db_message`/`classify_db_error`，纯函数）；`server.py::agent_error` 是 **agent 侧唯一错误出口**，`admin.error_payload` 是查询台的（额外给连接类错误打 `error_kind` 驱动重连按钮）
- `notify.py` 通知抽象：`Notifier` + `NoopNotifier`（默认）+ `MacOsNotifier`（osascript）+ `WebhookNotifier`（Bark/企微/飞书三个 provider 模板）+ `CompositeNotifier`（并发多路，单渠道失败不阻断）+ `NotifierRouter`（每次 send 读最新 settings 动态组装 → 改配置即时生效不重启）；`build_from_settings(settings, inbox)` 把设置项翻译成 Composite；`_cmd_serve` 才注入真路由
- `inbox.py` 站内通知收件箱：`InboxStore`（SQLite `notification` 表，7 天保留）+ `InboxNotifier`（写库并通过内存 fan-out 推给 SSE 订阅者）；后台铃铛 SSE 走这里；管理后台**默认渠道恒开、不可关**
- `drivers/` 可插拔数据库驱动（`base.py` 的 `DbDriver` 基类 + `DRIVERS` 注册表；mysql/postgres/sqlite/clickhouse/duckdb 各一个模块，模块级 `@register` 即注册）：把「一种引擎」封装成一个适配单元——建连、运行期取消、DDL、容量/行数估算、跨库搜表、DB 侧语法复核、执行计划前缀、默认端口、品牌图标、sqlglot 方言、能力声明（`has_schema_layer`/`sync_target`/`ai_sql`）。**engines.py 只留引擎无关的执行/反射逻辑，引擎特有入口一律 `get_driver(engine_kind).xxx(...)` 委托。** 加一种数据库 = 在 `drivers/` 加一个模块、在 `drivers/__init__` 加一行 import：`config.engine` 已是 str（配置层不校验清单），连接表单下拉/默认端口/图标、同步源目标清单、编辑器 lint 方言、SQL 美化全部从注册表派生（`tests/test_drivers.py` 用临时注册的假引擎端到端钉住这条链路）。**驱动可以自己写，也可以用成熟驱动库**：关系库几乎零代码（SQLAlchemy dialect 覆盖 MySQL/PG/SQLite/ClickHouse/MSSQL/Oracle…，`build_engine` 就是拼 URL + 会话设置事件），没有现成方言的库自己实现接口的几个方法即可（`redis_engine.py` 是先例：它有自己的池、不在注册表里，连接表单的引擎下拉单列它）。**依赖方向**：drivers 模块级绝不 import engines（只在校验方法内惰性 `from .. import engines`），engines 模块级 import drivers——import 无环；`create_engine`/`event` 一律经 `engines.xxx` 引用，保持单一引用面（测试 patch `dbmcp.engines.create_engine` 才有效）。**privileges（GRANT/REVOKE）与 checkup（体检）仍是各自的分发表**：它们需要引擎专属 SQL，不随驱动自动获得，加引擎时得另写
- `engines.py` SQLAlchemy 适配 + 引擎池（托管 SSH 隧道、reader/writer 双角色、空闲回收）。池 key = `(project, connection, role, schema, database)`——**database 维度是 PG 多库用的**：PG 一条连接只绑一个库，换库只能换连接（`list_server_databases` 查 `pg_database` 列库）
- `tunnel.py` 系统 OpenSSH 多跳隧道（结构化 `JumpHost` 链，每跳可带独立证书→按需生成 `ssh -F` 临时配置，否则走 `-J`；`resolve_jump_hosts`/`build_ssh_config` 纯函数）；`metadata.py` 元数据缓存（TTL）
- `audit/classify.py` 只读判定 + 指纹；`audit/risk.py` 风险评估；`audit/log.py` 操作审计（`audit_log` 表，含 `change_id` 关联审批单 + `agent_session` 会话元信息表：`upsert_session`/`get_session`/`list_sessions`（支持 since/until/keyword/project/connection/only_with_writes 筛选）+ `parse_time_filter` 纯函数把「本地日期」归一成 UTC ISO；`_WRITE_TOOLS` 定义"需审批的写"工具集，供审计页读/写过滤）
- `approvals.py` 审批单存储与生命周期（含 `exec_result` 执行结果回填、`rollback_note` 回滚参考、批量取单 `get_many`）；`admin/` 管理后台包（Starlette custom_route，服务端渲染）：`common.py` 认证/本机来源校验/HTML 原语/页面外壳（`_page(theme=)`）· `context.py` `AdminContext`（原 mount_admin 闭包里共享的 service/guard/_shell/_theme/JobManager…）· 各路由模块 `auth` `dashboard` `exports` `approvals`（含通知一次性审批链接 `/act`）`audit` `connections` `static` `console`（`/admin/sql/*`）`redis` `privileges` `settings` `notifications` `workflows`，每个暴露 `mount(ctx)`；`__init__.py` 的 `mount_admin` 按序挂载并再导出测试用到的名字
- **审批等待**（`server.py::_wait_for_decision`/`_wait_then_execute`）：agent 的 `execute` 首提生成审批单后**在服务端等人决策**（`approval_wait_seconds` 设置，默认 120s，工具参数 `wait_seconds` 可覆盖，0=不等），批准即自动核销执行；独立工具 `wait_for_change` 供超时后续等。后台 `service.approve_and_execute_change` = 「批准并立即执行」
- `jobs.py` 后台查询台异步任务管理器（`JobManager`）：按 queue_key **忙时拒绝（Busy）** + 计时 + 取消（DB 层 KILL）；纯逻辑可单测（test_jobs.py）。admin 的 `/admin/sql/{run_async,job,cancel}`、workflow/画布 run 都走它
- `redis_engine.py` Redis 池（key 含 db 维度，支持 Medis 式切库；无认证=密码引用为空传 None）+ 浏览纯函数 `keyspace_dbs`（列全部逻辑库 db0..N-1，库数取 CONFIG databases 回退 16，有数据的带键数）/`scan_keys`（SCAN+TYPE，不阻塞）/`read_value`（按类型取值 + TTL/内存/编码）；`audit/redis_rules.py` 命令分类；`masking.py` 脱敏（内置词表按列名猜 + `mask_columns` 点名；**是否启用内置词表可配**——全局设置 `mask_sensitive_columns` + 连接级 `Policy.mask_default_patterns` 三态覆盖，`resolve_default_patterns` 定值）；`__main__.py` serve/approvals/approve/reject 子命令；默认路径按运行形态选（源码目录 `config/` `data/`，否则 `~/.config/db-manage-mcp/`，`DBM_HOME` 可挪），配置缺了从包内 `connections.example.yaml` 生成，启动时读 `~/.config/db-manage-mcp/env`、没有 `DBM_ADMIN_TOKEN` 就生成并存进去，`show_banner=False` 不打 FastMCP 横幅
- `redis_docs.py` Redis 命令文档数据集（176 条命令→摘要/语法/分组/redis.io 链接，`lookup()` 首 token 大写查；命令窗口文档面板用，Haiku 生成）
- **Redis 控制台**（对标 Medis，`/admin/redis` + `static/redis.*`，独立 Vue 应用）：左树=键（前缀分组，点文件夹**深度优先只展第一个子文件夹这一条链钻到底**、再点收起整棵；不做广度展开防刷屏），底部库切换器列逻辑库（`redis_databases`→`keyspace_dbs` 列到 `max(16, 最大非空库号+1)`，`MIN_DBS_SHOWN=16`，库数取 CONFIG databases；有数据的库始终在、带键数）→键（`:` 前缀分组文件夹 + STRING/HASH/LIST/SET/ZSET 彩色徽章）；中=命令窗口（Monaco，选中/光标行 ⌘⏎ 执行，`admin_redis_run` 读直通/写返回风险确认→writer 直接执行审计 admin_execute，**后台旁路不进审批单**；**prod 写命令须额外输入连接名（confirm_text）匹配才放行**，`CONFIG GET`/`ACL` 结果经 `redact_command_result` 脱敏密码/口令哈希——密钥不落返回值红线）+ 结果区/键详情双 tab；右=命令文档面板（随光标命令自动切、链 redis.io）。service `redis_databases/redis_keys/redis_value/admin_redis_run`（都过 `_audited`，tool=redis_keyspace/redis_scan/redis_read/redis_command|admin_execute）；admin `/admin/redis/{databases,keys,value,run,command-doc,connections}`。**Redis 连接从查询台下拉过滤掉**（console.js connOptions），改由此页承载。无认证连接：config validator 只要求 host、`probe_connection_fields` 豁免 redis 密码守卫
- `privileges.py` 数据库用户与权限管理（PG / MySQL）：目录查询 SQL + DCL 构造，**纯函数**。所有成分先过白名单/字符校验再按方言引用（语句由服务端生成，页面传不进任意 SQL）；带密码的语句返回 `DclStatement(sql, audit_sql)` 两份，审计只落脱敏版。service `admin_list_db_users/admin_db_user_grants/admin_privilege_matrix/admin_run_dcl`（用 writer 账号，审计 tool=`admin_privileges`/`admin_dcl`）；admin 只提供 `/admin/privileges/*` JSON 接口，**没有独立页面**——UI 是 `static/privileges.js` 里的 `window.PrivPanel` 组件（同 dg-select 的外部注册做法），由 console.js 注册成 `<priv-panel>`，入口在**左树「库/连接」右键菜单**「用户与权限…」。**不暴露为 MCP 工具**（同红线 5）
- `sync.py` 跨连接表同步的纯函数（`SyncSpec` 计划模型 + `validate_spec`/`spec_fingerprint`/`build_select_sql`/`rewrite_ddl`/`render_plan`/`assess_plan`）：同引擎用源库建表语句原文、跨引擎用 sqlglot 转写成**近似 DDL** 并剥掉方言私有成分。service `sync_table`（计划 → 审批单 → 带 change_id 核销执行，审计 tool=`sync_read`/`sync_write`）；MCP 工具 `sync_table`
- `export.py` 查询结果导出纯函数（CSV/JSON/Markdown/xlsx；openpyxl 惰性导入）
- `static/` vendor 的前端资源：`vue.global.prod.js`（Vue 3 全局构建，免打包）、`monaco/vs/`（Monaco 编辑器，~13MB）、`console.css`/`console.js`（查询台 Vue 应用）。由 admin `/admin/static/{path:path}` 路由服务（支持子路径，**不加 @guard**——见教训「Monaco 无构建」）
- `analysis.py` 分析工作台（DuckDB 沙箱，设计见 ANALYSIS.md）：每工作区一个 data/analysis/<ws>.duckdb；**沙箱内任意 SQL 自由执行不需审批（本地草稿纸），从源库取数走 _read（reader+审计+行数上限，默认 20 万/硬上限 50 万）**。service `analysis_import/analysis_sql/analysis_overview`（审计 project="analysis"）；查询台把工作区当连接用（conn="analysis/<ws>"，admin 各路由 `_analysis_ws()` 分流）；右键表「导入到分析工作区…」内联条；MCP 工具 `analysis_workspaces/analysis_import/analysis_sql` 开放给 agent（跨源 JOIN 的正确姿势——计算下推，上下文只带小结果）。DuckDB 连接非线程安全 → 每次操作短连接
- `examples.py` 首次启动播种：`data/demo/shop.sqlite3` 示例库（customers/orders，固定随机种子）+ 示例流程「渠道ROI分析」（取数节点引用 `demo/shop`）+ 成本 CSV
- `snippets.py` SQL 片段库（SnippetStore，存 dbm.sqlite3 的 sql_snippet 表；标题+备注+SQL+连接）；service `list/save/delete_snippet`；admin `/admin/sql/snippets*` 路由
- **查询台**（DataGrip 风深色 IDE，`admin/console.py` `/admin/sql*` + `static/console.*`）：Vue 3 + Monaco 编辑器，三栏布局（左树:连接/表就地展开/片段 · 多 SQL tab 编辑器 · 底部结果），**少弹框/一屏**（表结构就地展开、片段左栏、写确认内联条，均非弹框）。页面只给挂载点，逻辑在 `static/console.js`，数据全走 `/admin/sql/*` JSON 接口（connections/tables/table/run/format/export/snippets）。**后台专属写入路径 `service.admin_run_sql(confirm=)`**：读直跑 reader；写未确认返回风险报告、确认后用 writer **直接执行**并审计（tool=`admin_execute`），**不进审批单**——后台旁路，「拒绝—重提」红线只约束 agent 的 execute。导出仅限只读语句。DB 触达路由用 `anyio.to_thread.run_sync` 卸载，不阻塞同 ASGI 上的 agent
- `connections.py` 连接管理（写回 YAML + 密码进 keyring + SSH key 校验）；`admin/auth.py` + `admin/context.py` 后台认证（token→hmac cookie，guard 保护路由）+ `admin/connections.py` 连接管理页
- 连接热加载：ConnectionManager 直接改 service 持有的 AppConfig 对象（同一引用），并 dispose_connection 回收旧引擎/隧道，无需重启或重新 load_config
- elicitation 快捷审批在 `server.py::_maybe_elicit_approval`：审批单先创建（审计完整），elicitation 只是把批准动作搬进会话；客户端不支持时异常被吞、自然回退审批单流程

## 当前状态

已完成功能的实现细节按时间倒序记在 `docs/HISTORY.md`，用户可见的变更在 `CHANGELOG.md`。仍未做的：

- [ ] 前端模块化 Stage 2/3/4（**待办，非 bug 修复，纯代码组织优化，可随时做也可不做**）：
  - **Stage 2**（最大、风险最高）：`console.js`（~2960 行单个 Vue Options 组件）按 ES module 拆分（状态/编辑器/tabs/树/结果/书签/片段/工作流/图表/子组件）+ Vue 模板抽成单独文件。项目无构建，用浏览器原生 ES module，不引入打包器。**风险点**：`editor/models/monaco` 等闭包状态被上百个方法共享，拆完须逐功能全量浏览器回归（⌘S/书签/改名/手风琴/分页/图表/DAG…）。**建议单独开一个专注会话做**，别在上下文吃紧时半路拆
  - **Stage 3**（机械、低风险）：`console.css` 拆成逻辑 partial；顺手删已失效空转的旧补丁（`.dg-rt th`、`.quick-input-widget label{margin:0}`、`.dg-res-meta .pager{margin:0}` —— Stage 1 已从根上消除渗漏，这些压制规则不再需要）
  - **Stage 4**（中等、低风险）：`admin/` 各模块剩余内联抽离——登录页 `<style>`→`login.css`、redis/审计/设置/连接页的内联 `<style>`/HTML 静态部分抽走（admin.py 拆包已完成，见 `admin/`）。**注意**：动态 HTML 因当初刻意不用 Jinja2/HTMX（YAGNI，见 DESIGN.md），仍需 Python f-string 拼装，只抽**静态**样式、保留最小动态拼装逻辑
- [ ] goInception 可选集成：**有意未做**——需要跑起 goInception 实例才能联调，不写无法验证的集成代码；接入点预留在 audit/risk.py（MySQL 深度审核可作为 assess 的增强数据源）

## 真实集成验证状态

- ✅ MySQL 9.5（本机）、PostgreSQL 17（Docker）、Redis 7（Docker）、SSH 两跳隧道（scripts/e2e_ssh_multihop.sh）均已真实 e2e 通过。
- writer 账号的"仅审批通过才使用"依赖 config 正确配置独立账号；sqlite 无账号概念，测试里 writer 复用同库。
- 空闲回收 + 保留期清理由 `service.start_housekeeping()` 驱动（serve 时启动，60s 一轮，保留期默认 30 天可由 `--retention-days`/`DBM_RETENTION_DAYS` 配）。

