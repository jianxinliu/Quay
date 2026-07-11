# db-manage-mcp

给所有 agent 提供数据库访问的 MCP 服务：按项目管理连接与账密、SSH 多层跳板、SQL 审计与人工授权、操作审计与管理后台。完整设计见 `DESIGN.md`（改动核心流程前先读它）。

## 技术栈（已确认，勿擅自更换）

- Python 3.12 + FastMCP（官方 MCP SDK）+ FastAPI（MCP 与管理后台共用一个 ASGI 应用）
- sqlglot 做 SQL AST 解析与审计；SQLAlchemy Core（不用 ORM）+ redis-py
- SQLite 存审计/审批单/元数据缓存；管理后台服务端渲染 HTML（未用 Jinja2/HTMX）
- SSH 多跳复用系统 OpenSSH（`ssh -J jump1,jump2`），不自己实现隧道协议
- 部署形态：**本地进程模式**（`uv run dbm serve`），macOS 用 launchd 常驻（scripts/install-launchd.sh）；stdio 作单 agent 直连模式。**已弃用 Docker**——本地单机场景 Docker 只带麻烦（连宿主库绕网络、无 keyring 后端、SSH key 变容器内路径）

## 安全设计红线（实现时必须遵守）

1. **默认拒绝**：SQL 解析失败、多语句、无法分类的一律按写操作处理，进审批流。
2. **密钥不落明文**：配置文件只存 `env://` / `keyring://` 引用；密码永不出现在日志、审计记录、工具返回值中。本地进程模式下管理后台可把页面输入的密码写 keyring；`env://` 引用的值由常驻服务从 `~/.config/db-manage-mcp/env`（600）注入。
3. **拒绝—重提 + change_id 放行**：写操作先被明确拒绝并生成审批单（含风险报告），人在后台批准后 agent 带 change_id 重提才放行；**执行的永远是审批单里存储的 SQL**，重提文本只作指纹校验（不一致即拒），指纹匹配仅作无 id 时的兜底。审批单一次性核销、有 TTL（30 分钟）。prod 环境强制审批。
4. **双账号**：日常查询走只读账号，仅审批通过的执行切换 writer 账号。
5. **连接与密钥管理不暴露为 MCP 工具**（agent 碰不到）；人可通过 CLI 或**已登录的管理后台**操作。后台需认证（DBM_ADMIN_TOKEN），页面输入的密码一律进 keyring、配置只存引用。
6. 通知遵循"安静即正常"：不主动推送，审批挂起由 agent 在会话中告知用户。

## 约定

- 每条 SQL 必须落审计记录：agent（clientInfo + session）、时间、连接、SQL 原文与指纹、风险等级、结果（行数/耗时/状态）、审批信息。
- 查询默认加 LIMIT（1000）与语句超时（30s），可按连接配置。
- Redis 走命令分类模型：读命令直通；KEYS / FLUSHDB / FLUSHALL / CONFIG 属 CRITICAL 需审批。

## 开发

```bash
uv sync --extra keyring    # 安装依赖
uv run pytest              # 全量测试（改动后必须全过）
DBM_MYSQL_PW=... DBM_ADMIN_TOKEN=... uv run dbm serve   # HTTP（127.0.0.1:8100）
uv run dbm serve --stdio                                # stdio 模式
bash scripts/install-launchd.sh                         # macOS 常驻（幂等，改配置后重跑即热重启）
```

- 代码在 `src/dbmcp/`，分层：`server.py`（MCP 接口）→ `service.py`（核心逻辑，与传输解耦、可直接单测）→ `engines.py` / `audit/`（分类器 + 审计日志）。新增工具时逻辑写在 service 层，server 层只做注册和 ToolError 转换。

## 经验教训

- **包名是 `dbmcp`，不是 `dbm`**：`dbm` 是 Python 标准库模块，会被 stdlib 遮蔽导致 import 全挂。CLI 命令仍叫 `dbm`。
- **本机测本地服务要绕过代理**：这台机器 shell 有 SOCKS 代理环境变量，不绕过会把 127.0.0.1 请求发进代理得到 502。curl 用 `--noproxy '*'`；fastmcp Client（底层 httpx）用 `env NO_PROXY='*' no_proxy='*'`（单纯 `-u ALL_PROXY` 不够，httpx 还会读其他代理变量，NO_PROXY 最稳）。
- **写 sqlglot 相关逻辑前先跑实验脚本验证解析行为**：已验证的坑——PG 方言 `EXPLAIN`/`SHOW` 退化为不透明 `Command` 节点需特判；`WITH x AS (INSERT ...) SELECT` 顶层是 `Select`，必须遍历整棵树找写节点。
- fastmcp 3.x 装的是 `fastmcp` + `fastmcp-slim`，其依赖 `uncalled-for` 是正常的依赖注入库（已核实非投毒包）。
- **MySQL 会话设置的多条语句不能逗号拼接**：`SET SESSION max_execution_time=X, SESSION TRANSACTION READ ONLY` 是非法语法（1064）——`SET TRANSACTION READ ONLY` 是独立语句、不是变量赋值。必须用 connect 事件监听器逐条 execute。此 bug 只有真实 MySQL e2e 才暴露，SQLite 单测发现不了；已抽 `mysql_session_statements()` 纯函数 + 回归测试（test_engines.py）。**教训：DB 方言相关代码，SQLite 单测不够，必须对目标 DB 跑真实 e2e。**
- 真实 MySQL 9.5 e2e 已验证通过：reader 只读防线报 1792、writer 双账号写入、information_schema 行数估算、索引命中风险判定、完整拒绝—重提审批闭环、管理后台批准。
- **防「大表拉挂 DB」用注入 LIMIT，别用流式游标**：pymysql 默认缓冲游标会把整个结果集读进客户端内存，`SELECT * FROM 大表` 无 LIMIT 时把进程/DB 拖挂。试过 `stream_results=True`（SSCursor）——但提前 fetchmany 后关连接、连接归还池复用会报 `Previous unbuffered result was left incomplete` / commands out of sync。**正解：给缺 LIMIT 的顶层 SELECT/UNION 注入 `LIMIT max_rows+1`（`engines.paginate_sql`，sqlglot AST，用户自带 LIMIT/非 SELECT 不动），让 DB 只返回这么多行，缓冲游标即安全、池复用干净**。查询台在此基础上做 LIMIT/OFFSET 分页（取 page_size+1 探测下一页，走 `_read` 不受连接 max_rows 二次截断）。
- **未绑定 database 的 MySQL 连接不能直接反射**：SQLAlchemy `inspect(engine).get_table_names(schema=None)` 在无默认库时，取 `SELECT DATABASE()` 得 None，`_escape_identifier(None)` → `None.replace(...)` 崩（报 `'NoneType' object has no attribute 'replace'`）。修法：查询台对未绑库的 MySQL/PG **先列库**（`engines.list_databases` = `inspect().get_schema_names()` 过滤系统库），选库后 `get_table_names(schema=db)` / `describe_table(table, schema=db)` 带 schema 反射；`service.list_tables(schema=None)` 对无库连接直接抛清晰错误而非让它崩。这也是 DataGrip 的库→表→列三级树。
- **子进程活着时别 read() 它的管道**：SSHTunnel 隧道就绪超时路径先 `stderr.read()` 再 close——ssh 还活着时 read() 阻塞到进程自行退出（DNS 挂起时数分钟），把探测/全量测试整个卡死；且该 bug 被网络环境掩盖（ssh 秒败走"提前退出"分支不触发），只在 DNS 变慢的日子爆发。正解：**先 terminate/kill 子进程再 read**（进程死后 read 立即 EOF 且保留已缓冲内容）。排查特征：进程 CPU 时间停住不涨、wall clock 在走 = 阻塞在外部 IO。
- **后台任务线程必须 daemon**：admin 异步查询最初用 `ThreadPoolExecutor`——它的工作线程**非 daemon**，每个 test_admin 用例 mount_admin 都建一个池，pytest 用例全过却在退出时挂死（表现为"全量测试跑不完"，极难定位）。改为 `threading.Thread(daemon=True)` 逐任务起线程。凡常驻服务里的辅助线程（housekeeping、job worker）一律 daemon。
- **Monaco 无构建 vendoring 的坑**：① Monaco 的 AMD loader 运行时按路径拉几十个文件，静态路由必须支持子路径（`{path:path}`）；② web worker 用 data-URI `importScripts(workerMain.js)` 加载，若静态路由带 `@guard`，worker 请求带不上 cookie 会被 303 到登录页而崩——所以 `/admin/static/*` **不加鉴权**（都是公共库文件、无敏感数据）；③ 用 `automaticLayout:true` 让 Monaco 自适应容器尺寸，省去手动 layout。装法：`npm registry` 取 `monaco-editor` tgz 解 `package/min/vs` 到 `static/monaco/vs`。
- **前端全屏页别用 `position:absolute;inset:0` 铺满**：会相对视口定位、盖住后台左侧导航栏。用 `height:100vh` 让它待在 `main` 网格列内即可。
- **自家静态文件（console.js/css）必须 `Cache-Control: no-cache`**：曾设 max-age=86400，迭代时浏览器一直跑旧 JS，连坑三次（新功能"没生效"、旧 bug"复现"，其实全是缓存）。现在静态路由区分：vendor（monaco/、vue.*，内容不变）长缓存，其余 no-cache（每次带 If-None-Match 校验，改完即生效，无需硬刷新）。
- **合成打字对代码编辑器（CodeMirror/Monaco）不可靠**：浏览器自动化 `type` 会掉字符；e2e 验证时用 `javascript_tool` 直接 `monaco.editor.getModels()[0].setValue(...)` 或 CM 的 `setValue`。
- **UMD 库必须放在 Monaco loader.js 之前加载**：loader.js 定义了全局 `define.amd`，其后加载的 UMD 包（如 echarts）会走 AMD 注册而**不挂 window.xxx**，表现为"脚本 200 加载成功但全局变量 undefined"。修法：`<script>` 顺序上把 UMD vendor 放到 loader.js 前面（见 admin.py `_console_body`）。
- **sqlglot 的 TokenizeError 不是 ParseError**：引号不闭合等词法错误在**分词阶段**抛 `TokenizeError`，不是 `ParseError`；二者都继承 `sqlglot.errors.SqlglotError`。`classify`/`assess` 只 catch `ParseError` 会让非法 SQL 直接抛异常冒泡（agent 的 execute 走 server.py 只 catch QueryRejected/KeyError/ValueError → 变 500；违反「默认拒绝」红线）。**必须 catch 基类 `SqlglotError`**，任何解析/分词失败都默认拒绝按写操作/最高风险处理。回归测试见 test_classify.py::test_tokenize_error_rejected_not_raised、test_risk.py::test_tokenize_error_is_critical_not_raised。
- **ProxyJump 的两个坑（多跳 e2e 踩出）**：① 命令行 `-o` 选项只作用于最终目标、不传递给跳板连接——自定义密钥/known_hosts 必须用 `-F 配置文件`（对链上每跳生效），对应 `ssh_options: ["-F", path]`；② sshd_config 是**首个匹配生效**，往文件末尾 append `AllowTcpForwarding yes` 无效，容器里用 `sshd -o` 命令行覆盖。多跳验证脚本：`scripts/e2e_ssh_multihop.sh`（两级 sshd 容器链到本地 MySQL，凭证全程隔离在 /tmp）。

## 模块地图（src/dbmcp/）

- `server.py` MCP 工具注册 → `service.py` 核心逻辑（可单测）
- `engines.py` SQLAlchemy 适配 + 引擎池（托管 SSH 隧道、reader/writer 双角色、空闲回收）
- `tunnel.py` 系统 OpenSSH 多跳隧道；`metadata.py` 元数据缓存（TTL）
- `audit/classify.py` 只读判定 + 指纹；`audit/risk.py` 风险评估；`audit/log.py` 操作审计
- `approvals.py` 审批单存储与生命周期；`admin.py` 管理后台（Starlette custom_route，服务端渲染）
- `redis_engine.py` Redis 池；`audit/redis_rules.py` 命令分类；`masking.py` 脱敏；`__main__.py` serve/approvals/approve/reject 子命令
- `export.py` 查询结果导出纯函数（CSV/JSON/Markdown/xlsx；openpyxl 惰性导入）
- `static/` vendor 的前端资源：`vue.global.prod.js`（Vue 3 全局构建，免打包）、`monaco/vs/`（Monaco 编辑器，~13MB）、`console.css`/`console.js`（查询台 Vue 应用）。由 admin `/admin/static/{path:path}` 路由服务（支持子路径，**不加 @guard**——见教训「Monaco 无构建」）
- `analysis.py` 分析工作台（DuckDB 沙箱，设计见 ANALYSIS.md）：每工作区一个 data/analysis/<ws>.duckdb；**沙箱内任意 SQL 自由执行不需审批（本地草稿纸），从源库取数走 _read（reader+审计+行数上限，默认 20 万/硬上限 50 万）**。service `analysis_import/analysis_sql/analysis_overview`（审计 project="analysis"）；查询台把工作区当连接用（conn="analysis/<ws>"，admin 各路由 `_analysis_ws()` 分流）；右键表「导入到分析工作区…」内联条；MCP 工具 `analysis_workspaces/analysis_import/analysis_sql` 开放给 agent（跨源 JOIN 的正确姿势——计算下推，上下文只带小结果）。DuckDB 连接非线程安全 → 每次操作短连接
- `snippets.py` SQL 片段库（SnippetStore，存 dbm.sqlite3 的 sql_snippet 表；标题+备注+SQL+连接）；service `list/save/delete_snippet`；admin `/admin/sql/snippets*` 路由
- **查询台**（DataGrip 风深色 IDE，admin.py `/admin/sql*` + `static/console.*`）：Vue 3 + Monaco 编辑器，三栏布局（左树:连接/表就地展开/片段 · 多 SQL tab 编辑器 · 底部结果），**少弹框/一屏**（表结构就地展开、片段左栏、写确认内联条，均非弹框）。页面只给挂载点，逻辑在 `static/console.js`，数据全走 `/admin/sql/*` JSON 接口（connections/tables/table/run/format/export/snippets）。**后台专属写入路径 `service.admin_run_sql(confirm=)`**：读直跑 reader；写未确认返回风险报告、确认后用 writer **直接执行**并审计（tool=`admin_execute`），**不进审批单**——后台旁路，「拒绝—重提」红线只约束 agent 的 execute。导出仅限只读语句。DB 触达路由用 `anyio.to_thread.run_sync` 卸载，不阻塞同 ASGI 上的 agent
- `connections.py` 连接管理（写回 YAML + 密码进 keyring + SSH key 校验）；admin.py 后台认证（token→hmac cookie，@guard 保护路由）+ 连接管理页
- 连接热加载：ConnectionManager 直接改 service 持有的 AppConfig 对象（同一引用），并 dispose_connection 回收旧引擎/隧道，无需重启或重新 load_config
- elicitation 快捷审批在 `server.py::_maybe_elicit_approval`：审批单先创建（审计完整），elicitation 只是把批准动作搬进会话；客户端不支持时异常被吞、自然回退审批单流程

## 当前状态

- [x] 设计定稿（DESIGN.md）
- [x] M1 骨架：daemon + SecretProvider + MySQL/PG/SQLite 只读 query + schema + 操作审计
- [x] M2 连接：SSH 多跳隧道 + 引擎池（隧道托管/空闲回收）+ 元数据缓存
- [x] M3 管控：风险审计引擎 + 拒绝—重提审批流（change_id 放行、writer 双账号）+ 管理后台
- [x] M4 增强：elicitation 快捷审批（local/dev 默认开，审批单始终留痕）+ Redis 适配（命令分类 + 同一套审批流，真实 Redis 7 e2e 通过）+ 敏感字段脱敏 + CLI 审批兜底（dbm approvals/approve/reject）。130 个测试全过。
- [x] M6 查询台（DataGrip 风深色 IDE）：Vue 3 + Monaco，三栏布局，少弹框/一屏。左树支持**库→表→列三级**（未绑库连接先列库，避免 no-database 反射崩溃）+ 可拖动分隔条（左栏宽/编辑区高）+ 多 SQL tab（各自独立 model）+ 列名补全（`别名.`/`库.表` → 字段，按 schema 取列）。读查询 + 写二次确认（内联条）后 writer 直接执行（后台旁路，不进审批单）+ CSV/JSON/Markdown/xlsx 导出 + SQL 片段库（存 dbm.sqlite3，加载即执行）。真浏览器 e2e 全通过（含真实 MySQL 库/表/列树、拖动、列名补全、结果分页、tab 拖拽、schema 筛选）。结果分页 + 自动 LIMIT 兜底（防大表拉挂 DB）+ tab 拖拽排序 + 库多时 schema 筛选框。217 个测试全过。导入**有意未做**（批量写风险面大，下一期专门设计）
- [x] M6x5 查询台桌面化打磨：**自绘下拉组件 dg-select**（原生 select 弹出列表无法 CSS 定制，连接/schema 选择器均换用，带筛选）+ **异步查询任务**（/admin/sql/run_async 提交到服务端线程池 + /admin/sql/job 轮询；job_id 持久化，**切页/刷新不中断查询**，回来自动续接取结果，结果保留 10 分钟）+ 查询台页**左导航默认收起 46px**（hover 浮出展开，console.css 纯 CSS）+ 树图标换彩色 SVG（青柱库/金格表/蓝文件夹/琥珀钥匙/紫索引，data-URI）+ **ORDER BY 自由表达式输入框**（与列头点击联动，支持多列/函数）+ tab **pin**（钉住无 ✕ 防误关）+ 统计行数改 toast（修 bug：原来开 data tab 被 buildDataSql 覆盖成 SELECT *）+ 编辑区状态条与 Monaco 首行对齐（19px 行高）+ **审计页默认隐藏 agent=admin-ui**（切换链接；AuditStore 支持 `col__ne` 排除条件）。保活仍用 localStorage（关页面/关浏览器均保留；用户确认够用，服务端 SQLite 方案有意不做）
- [x] M6x4 查询台四项：**光标处执行**（多语句只跑光标所在条/选区，stmtRanges 处理引号注释；翻页用 readSql 不受光标移动影响）+ **data tab 的 WHERE 条与列头排序走 SQL**（DataGrip Table Data 行为：buildDataSql 拼 WHERE/ORDER BY 重查、跨页正确、列头 funnel 填 WHERE）+ **单元格就地编辑**（限 data tab；双击改值 → 按主键定位生成 UPDATE → 走既有写确认流 → writer 执行审计 admin_execute → 自动刷新当前页；无主键拒绝）+ **历史面板与 EXPLAIN 可视化**（历史来自审计按连接去重 /admin/sql/history；EXPLAIN 按方言 FORMAT=JSON（sqlite QUERY PLAN 行）→ plan-node 递归折叠树 + access_type/cost 徽章，纯 EXPLAIN 不执行语句）。ER 图**有意不做**（用户明确砍掉）。真实 MySQL e2e 四项全过。219 个测试全过
- [x] M6+++ 查询台 DataGrip 化三大补全：**全状态保活**（tab 含结果/树展开与元数据（每连接快照）/分隔尺寸 → localStorage v2，切页刷新原样恢复不重查；>3.8MB 丢结果保 SQL）+ **tab 三类**（query 编辑器+结果 / data 双击表打开·主区即数据网格·编辑器隐藏 / ddl 编辑区只读展示真实 SHOW CREATE TABLE，PG 反射拼近似 DDL）+ **DataGrip 式树**（库→tables(N)→表→columns/keys/indexes(N)，箭头展开、点名选中、⌘/Ctrl 多选、右键菜单、**批量 DROP 红色内联确认条**——逐条 writer 执行并审计 admin_execute，真实 MySQL e2e 验证真删 2 张表）。刷新树保留展开路径并自动补拉已展开层。219 个测试全过
- [x] M6++ 查询台交互补全：**执行 schema 上下文**（右上角选择器；引擎池按 (project,connection,role,schema) 建独立引擎——MySQL 覆盖默认库/PG 设 search_path，比共享连接上 USE 干净；审计记录 schema=x）+ **双击表名开 tab 直接分页看数据** + **表右键菜单**（打开数据/SELECT/统计行数/看结构/建表语句/复制表名）+ **编辑区左侧执行状态条**（idle/转圈/✓/✗）+ 无 ORDER BY 分页提示（paginate_sql 返回 ordered）。后台页面重做：连接表单进弹窗、审计表全宽自适应 + 列序（连接|环境|agent|工具|SQL|状态|时间|结果）、全站时间本地格式化（_fmt_ts）、组件去原生观感（下拉自绘箭头/checkbox 主题色/删除二次确认替代 confirm）。真浏览器 e2e 全过
- [x] P2 workflow：provenance 自动记取数配方（工作区 __provenance 表）+ WorkflowStore（SQLite）+ workflow_run 重拉数据→逐步执行→输出预览（任一步失败即停）；查询台「存工作流」+ 左栏工作流区（▶重跑/载入/删）；MCP run_workflow 开放
- [x] P3 可视化：结果区「表格/图表」切换（ECharts 5.6 vendored → static/echarts.min.js，**须放 Monaco loader 前**，见教训「UMD」）。柱/折线/饼/散点 + X/Y 列 + 聚合（SUM/COUNT/AVG/MIN/MAX，前端按 X 分组）；默认配置猜测（X=首列、Y=首个数值列）；图表配置随 tab 保活、随 workflow 保存（analysis_workflow.chart 列，老库自动 ALTER 迁移），载入/▶重跑自动恢复图表视图；分页结果只画当前页（配置条有提示）。真浏览器 e2e 全过（柱/饼/SUM 聚合/刷新保活/workflow 闭环）。ANALYSIS.md 路线图 P1–P3 全部完成，仅 P4（联邦直查/定时/DAG 画布）可选未做。239 测试全过
- [x] 查询台 Value Editor：点单元格右侧展开编辑面板（Value/Record 双 tab），长值/JSON 舒适编辑 + JSON 美化/压缩 + 设为 NULL；保存生成按主键定位的 UPDATE，走既有写确认流（与双击就地编辑共用 makeUpdateSql）
- [x] P1+P1.5 分析工作台（ANALYSIS.md）：DuckDB 工作区 + 连接快照导入 + CSV/Parquet 文件导入 + 查询台集成 + 3 个 MCP 工具开放给 agent。P2 workflow / P3 可视化待做
- [ ] goInception 可选集成：**有意未做**——需要跑起 goInception 实例才能联调，不写无法验证的集成代码；接入点预留在 audit/risk.py（MySQL 深度审核可作为 assess 的增强数据源）

## 真实集成验证状态

- ✅ MySQL 9.5（本机）、PostgreSQL 17（Docker）、Redis 7（Docker）、SSH 两跳隧道（scripts/e2e_ssh_multihop.sh）均已真实 e2e 通过。
- writer 账号的"仅审批通过才使用"依赖 config 正确配置独立账号；sqlite 无账号概念，测试里 writer 复用同库。
- 空闲回收 + 保留期清理由 `service.start_housekeeping()` 驱动（serve 时启动，60s 一轮，保留期默认 30 天可由 `--retention-days`/`DBM_RETENTION_DAYS` 配）。
