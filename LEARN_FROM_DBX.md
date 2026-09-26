# 向 DBX 学产品化

对照对象：[t8y2/dbx](https://github.com/t8y2/dbx)（官网 [dbxio.com](https://dbxio.com)）。
调研日期 2026-09-16；Quay 基线 `main@38b0f7a`。

这不是功能清单对照，而是回答一件事：**同样时间附近起步、表面能力接近，为什么它看起来像一个产品，我们还像一个很强的本机工具。**

## 0. 先把定位钉死

| | Quay | DBX |
|---|---|---|
| 仓库创建 | 2026-07-10 | 2026-04-29（早约 10 周） |
| 此刻 | 0 star（尚未按开源产品投放） | ~19.8k star / ~1.9k fork / 250+ 贡献者 |
| 一句话 | **人和 agent 共用的本地数据库工作台，agent 写操作必须经人审批** | **25 MB 装 90+ 库的桌面客户端，顺带 MCP** |
| 形态 | 本机进程 + 浏览器后台（`uv run dbm serve`） | Tauri 2 原生桌面 + Docker Web + CLI + 独立 MCP 二进制 |
| 引擎 | MySQL / PG / SQLite / ClickHouse / Redis | 90+（native + JDBC Agent + 插件市场） |
| 对 agent 写 | 审批单 + 风险报告 + 一次性核销 + 双账号 | 三档 MCP 策略 + 生产保护确认；**文档明确不替代审批** |
| 许可证 | Apache-2.0 | Apache-2.0 |

DBX 自己的边界写得很清楚（[What is DBX](https://dbxio.com/en/docs/what-is-dbx)）：

> DBX is a database workbench … **it does not replace** database privileges, **auditing**, backups, **or change approval**.

这恰好是 Quay 的护城河。去拼 90 种库、ER 图、插件市场，是用自己的弱项打它的强项。该学的是**产品化**，不是产品品类。

## 1. 「完善」主要不是功能更多

两边查询台已经能对上：对象树、SQL 编辑器、结果网格、就地编辑、导出、AI 生成 SQL、Redis 独立页、SSH 隧道、生产警示。
Quay 在治理侧明显更深：审批闭环、reader/writer、全量审计与会话回溯、agent 结果配额、分析工作台 DAG、SSH 每跳独立证书。

DBX 看起来更「像产品」，差在这几层——按对 Quay 的杠杆从高到低。

### 1. 第一次成功的路径

DBX 的第一屏是安装，不是源码：

```bash
brew install --cask dbx          # macOS
scoop install dbx                # Windows
winget install t8y2.dbx
npx -y @dbx-app/mcp-server       # MCP，复制进 .mcp.json 即用
```

装完打开窗口 → 新建连接（可粘贴 DSN）→ Test → 开始查。MCP 读的是**已经配好的连接**，agent 侧零配置。

Quay 现在的第一屏是开发者仪式：

```bash
uv sync --extra keyring --extra tokenizer
cp config/connections.example.yaml config/connections.yaml
DBM_ADMIN_TOKEN=... uv run dbm serve
# 再按客户端各写一份 HTTP MCP 配置，超时还得改到 ≥180s
```

macOS 已有 `scripts/build-app.sh` 和 launchd，但那是「会用源码的人」的路径，不是产品路径。
README 引用了 `assets/screenshots/hero.gif` 等图，仓库里目前只有 `assets/icon.svg`——路过的人连「长什么样」都看不见。

**可学：** 把「clone 才能跑」收成「下载 / `uvx` / brew 就能打开窗口」。MCP 推荐姿势可以继续是常驻 HTTP（审批单必须对上后台），但**安装物**必须是一个东西，而不是一份开发环境。

### 2. 文档按「人要干什么」组织，并写清边界

DBX 有独立站点，目录是工作流不是模块名：

Write and run SQL · Browse and edit data · Explore schemas · Compare and migrate · MCP · CLI · Production safety

每一页都有：这是什么、怎么点、跨入口行为表、**做不到什么**。
「Production and Write Safety」把只读连接、生产保护、MCP 三档、AI Ask/Agent、数据库账号权限画成不能互相绕过的层。

Quay 的 USER_GUIDE / AGENT_GUIDE / DESIGN 对人很全，但对「第一次听说的人」是仓库里的三份长文。站点、按工作流切片、公开的能力边界表，都还没有。

**可学：** 不必先做华丽官网。先按工作流把 README 第一屏和下钻页拆开，并像 DBX 那样主动写「我们不替代什么 / 不覆盖什么」。

### 3. MCP 是设置页上的产品，不是一串工具名

DBX Settings → MCP 是权威策略，每次请求重载：

1. 哪些连接 agent 看得见（含「未来新建的连不连」）
2. 每条连接哪些库
3. 执行档：只读 / 安全写 / 高风险写（可按库覆盖）
4. 暴露哪些工具

再叠加连接只读、生产保护、数据库账号权限——任何一档都不能放宽上层硬限制。
另有 `dbx_get_schema_context`（一次给 LLM 一份紧凑 schema）、`dbx_open_session`（会话级 USE/临时表）、`dbx_execute_and_show` / `dbx_open_table`（结果弹回桌面 UI）。

Quay 的 MCP 在**语义**上更强（审批、配额、脱敏、语法预检），但人在设置里几乎看不到「此刻 agent 能碰哪些连接、写要不要等人」。人和 agent 共用连接，却缺少「agent 视野」这一层产品表面。

**可学（且贴合护城河）：**

- 系统设置加 **MCP 策略页**：连接白名单、每条连接默认只读还是走审批写、是否对 agent 隐藏。不是新安全模型，是把已有红线画成人看得懂的开关。
- `schema_context(connection, tables?)`：一次返回表/列/索引/注释的紧凑文本，替代 agent 的 `list_tables` × `describe_table` 循环。
- `open_table` / `execute_and_show`：agent 查完把结果/表打开到查询台。这是「人和 agent 共用工作台」目前缺的那一跃。

**不要学：** `dbx_add_connection` / `remove_connection` 暴露给 agent。连接与密钥管理不进 MCP 是红线 5。

### 4. 从别的工具迁过来的成本按小时计

DBX 可粘贴 `mysql://` / `postgres://` 解析字段；可从 Navicat `.ncx`、DBeaver、DataGrip `dataSources.xml` 导连接（密码多半要重填，文档写明了）。
还有加密配置导出、颜色/分组/钉住、`dbx doctor`。

Quay 连接表单已经分区重做过，但新建仍是手填；目标用户多半已经有 DataGrip / DBeaver 连接。迁入成本不降，产品再强也进不了日常。

**可学：** 粘贴 DSN；DataGrip/DBeaver 导入（密码进 keyring，配置只存引用）；`dbm doctor`（数据目录、token、launchd、8100、各连接探测、MCP 是否可握手）。

### 5. CLI 是给脚本和 Codex 的一等入口

```bash
dbx doctor
dbx connections list --json
dbx context local --tables users,orders
dbx query local "select 1" --json
dbx query local --allow-writes --allow-dangerous-sql   # 仍不能打穿生产保护
```

稳定退出码、stderr 错误、`--json`/`csv`。生产库即使两个 flag 都开也拒。

Quay 的 `dbm` 偏运维（serve / approvals / approve）。Codex 一类「会调 shell 不会配 MCP」的 agent，没有一条 `dbm query` 可用。

**可学：** `dbm query` / `dbm schema` / `dbm doctor`，默认只读，写走同一套审批（或明确拒绝并打印 approval_url）。不要做一套绕过审批的 CLI 快路。

### 6. 桌面客户端的「专业感」清单（按需，勿全抄）

这些让 DBX 看起来像 TablePlus/DataGrip，不是让它在治理上赢：

| DBX 有 | Quay 现状 | 建议 |
|---|---|---|
| 虚滚大结果网格 | 分页 + LIMIT 注入（有意，防拉挂 DB） | 分页策略对治理更正确；虚滚是体验加分，不是缺口 |
| Schema diff / ER / 血缘 | 无 ER（有意砍）；`sync_table` 给 agent 迁结构 | 给人做「两连接点选对比」比 ER 值；不要为对标去画 ER |
| 存储过程 / 函数 / 触发器浏览器 | 左树到表/列/索引 | PG/MySQL 生产库常用，值得做对象层 |
| 驱动管理 + 插件市场 | 引擎写死在进程里 | **不要**学 90 库；深度打磨现有 5 种 |
| 从 DBeaver 迁入、配置云同步 | 无 | 迁入 P1；云同步与「密钥不落明文」冲突，谨慎 |
| 桌面 deep link `dbx://` | 浏览器 URL + 审批 deeplink | 审批链已经有；原生协议等真做桌面端再做 |
| 自动更新 / 代码签名 | 无 | 发安装包时才需要 |
| 中英西三语官网 | README 中英 + 人/agent 两份指南 | 站点可后置；截图和第一屏不能后置 |

### 7. 开源投放是产品的一部分

DBX：独立文档站、HelloGitHub、GitHub Trending、赞助商墙、贡献者证书、brew/scoop/winget/Flatpak、CNB 国内镜像、`dbx-store` 插件仓。
Quay 的 Apache-2.0 / SECURITY / CONTRIBUTING / 英文 README 已经齐，但截图、PyPI/`uvx`、主页 URL 仍是 CLAUDE.md 里「开源 P1 待办」。0 star 首先是还没按产品发布，不是功能差了 19k 个 star 的距离。

## 2. 不要学什么

1. **不要做第 6、第 7 种引擎来「看起来完善」。** DBX 用 Rust native + JDBC Agent + 插件市场摊薄深度；Quay 的深度来自方言级 e2e（MySQL 2013、PG 跨库、DROP PARTITION）。摊薄等于丢掉现在能讲的故事。
2. **不要把连接增删暴露给 agent。**
3. **不要用确认框替代审批单。** DBX 对生产写是「每次确认」；Quay 是「单、指纹、核销、TTL、审计」。文档里他们自己说这替换不了 change approval。
4. **不要为了对标恢复 Docker 默认部署。** 本地 keyring + 宿主 SSH key 这条路已经否过。
5. **不要让 Redis 进 MCP。** DBX 给了 `dbx_execute_redis_command`；Quay 有意只给人。键值误操作面和 SQL 审批模型不合，保持旁路。
6. **不要把「星数」当路线图。** DBX 吃的是「免费跨平台客户端」存量市场；Quay 吃的是「把库交给 agent 之后谁来批」的新市场。

## 3. 建议的落地顺序（需你拍板）

只列对「产品观感」杠杆最高、且不破坏红线的项。

**P0 — 让第一次成功像产品**

1. 真实截图 / GIF 进 README（现在引用了但仓库里没有）。
2. 一条安装路径：`uvx --from git+… dbm serve` 或 brew/cask；首次打开浏览器进连接向导，而不是先改 YAML。
3. `dbm doctor`：目录权限、token、端口、launchd、连接探测、MCP handshake。

**P1 — 把「人和 agent 共用」做成人感觉得到的产品**

4. 设置页 **MCP 策略**：连接白名单 + 每条连接对 agent 的默认能力（只读 / 审批写 / 隐藏）。
5. `schema_context` 工具（紧凑、有表名白名单、有字符预算）。
6. `open_in_console`：agent 打开某表或把刚跑的 SQL 丢进查询台一个 tab。
7. 粘贴 DSN 建连接；DataGrip / DBeaver 导入。

**P2 — 专业客户端期望，择一两个做透**

8. 给人用的 schema diff（复用 `sync.py` 的计划渲染，不必上 ER）。
9. 左树补 routines / triggers。
10. `dbm query --json` 只读 CLI（写仍进审批）。

未列入的（ER、插件市场、90 库、配置云同步、Electron/Tauri 重写）默认不做，除非产品目标改成「替代 DataGrip」。

## 4. 和现有待办的关系

CLAUDE.md 里已经写过、且和这次结论重合的：

- 开源 P1：PyPI/`uvx`、截图、英文 README（英文 README 已有，**截图和 uvx 还没有**）
- 前端模块化 Stage 2/3/4（代码组织，不改变产品观感）
- goInception（审核增强，不是产品化）

这次多出来的、DBX 对照后才明确的：MCP 策略页、schema_context、open_in_console、DSN/迁入、doctor、只读 CLI。

## 参考

- 仓库 https://github.com/t8y2/dbx
- 产品说明 https://dbxio.com/en/docs/what-is-dbx
- 安装 https://dbxio.com/en/docs/getting-started
- 生产安全 https://dbxio.com/en/docs/production-safety
- MCP https://dbxio.com/en/docs/mcp
- CLI https://dbxio.com/en/docs/cli
- 连接导入 https://dbxio.com/en/docs/connection-import
