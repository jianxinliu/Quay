# 贡献指南 / Contributing

> English TL;DR: `uv sync --extra keyring` → `uv run pytest` (all must pass). Never run a
> test server on port **8100** (that's the live instance) — use another port + a temp
> data-dir. DB-dialect code must be proven with a **real** database e2e, not just SQLite
> unit tests. PRs are squash-merged; commit messages explain *why*.

欢迎贡献！Quay（包名 `dbmcp`、CLI `dbm`）是一个面向数据库的安全治理工作台。下面是把改动
做到可合并所需要知道的一切。核心架构与安全设计见 [`DESIGN.md`](DESIGN.md)，改核心流程前请先读它。

## 开发环境

```bash
uv sync --extra keyring     # 安装依赖（含可选 keyring）
uv run pytest               # 全量测试 —— 改动后必须全过
```

- **本机测本地服务要绕过代理**：若 shell 设了 SOCKS 代理，127.0.0.1 请求会被发进代理得到 502。
  `curl` 用 `--noproxy '*'`；Python/httpx 用 `env NO_PROXY='*' no_proxy='*'`。
- 起临时服务做测试：**必须换端口 + 换数据目录，绝不用 8100**。`8100` 是常驻正式实例、也是
  MCP 客户端连接的端口；在它上面另起测试服务会抢端口、且共用 `data/dbm.sqlite3` 污染正式数据。
  一律：`uv run dbm serve --port 8201 --data-dir /tmp/dbm-test-data`（端口任选非 8100），测完清理。

## 代码结构与分层

代码在 `src/dbmcp/`，严格分层：

```
server.py   MCP 工具注册（薄层，只做注册 + ToolError 转换）
   ↓
service.py  核心逻辑（与传输解耦、可直接单测）—— 新功能的逻辑写在这里
   ↓
engines.py  SQLAlchemy 适配 + 引擎池（reader/writer 双角色、SSH 隧道托管）
audit/      classify.py 只读判定 + 指纹 · risk.py 风险评估 · log.py 操作审计
```

新增 MCP 工具时：逻辑放 service 层，server 层只注册。完整模块地图见 [`DESIGN.md`](DESIGN.md)
与 `CLAUDE.md`。

## 测试要求

- 新功能要覆盖核心路径 **和至少一个失败路径**；修 bug 先写能复现该 bug 的测试。
- **DB 方言相关代码，SQLite 单测不够**：MySQL/PG 的行为（会话变量、超时、EXPLAIN 格式、
  权限）SQLite 测不出。必须对目标库跑真实 e2e，脚本在 [`scripts/`](scripts/)
  （如 `e2e_serial_cancel.py`、`e2e_ssh_multihop_percert.sh`）。
- 提交前跑全量 `uv run pytest`，不是只跑新加的用例。

## 代码规范（lint / 类型）

```bash
uv run ruff check src/ tests/     # 代码风格与常见错误（CI 阻塞，须为 0）
uv run ruff check --fix src/ tests/   # 自动修可修的
uv run mypy                       # 类型检查（当前是基线，CI 非阻塞）
```

- **ruff**：CI 会拦，PR 里 ruff 必须干净。项目豁免了 `E702`（单行分号紧凑语句是本项目
  胶水代码的惯用风格）。
- **mypy**：目前作为**基线、非阻塞**运行——存量报错多是有意的「guarded Optional」模式
  （store 声明为 `X | None`、方法内 `if None: raise` 兜底，mypy 跟不进运行时守卫）。
  新代码尽量不引入新的类型错误；欢迎 PR 逐步收紧存量。

## 前端约定（无构建）

- 项目**不用打包器**：Vue 3 全局构建 + Monaco 均为 vendored（`static/`）。改 `console.js`/
  `console.css` 后浏览器刷新即生效（自家静态文件 `no-cache`）。
- **合成打字对 Monaco 不可靠**：e2e 里用 `monaco.editor.getModels()[0].setValue(...)` 直接赋值。
- 改查询台/Redis 控制台等 SPA 后，跑一遍真实浏览器交互回归（白屏第一步永远看 console 报错）。

## 安全红线（PR 必须遵守）

- **默认拒绝**：SQL 解析/分词失败、多语句无法分类的一律按写操作/最高风险处理。
  catch 用基类 `sqlglot.errors.SqlglotError`（TokenizeError/ParseError 都是它的子类）。
- **密钥不落明文**：配置只存 `env://`/`keyring://` 引用；密码/凭证**永不**出现在日志、
  审计记录、工具返回值中。
- **连接与密钥管理不暴露为 MCP 工具** —— agent 碰不到；只有人可通过 CLI 或已认证后台操作。
- 发现漏洞请走 [`SECURITY.md`](SECURITY.md)，**不要**开公开 issue。

## 提交与 PR

- 分支开发，别直接推 `main`。
- **提交信息说明「为什么改」**，而不只是「改了什么」。
- PR 统一 **squash & merge**。
- CI（若已配）与本地全量测试都要绿。

有拿不准的设计取舍（新增依赖、改公共 API/schema、大规模重构、安全相关改动），先开 issue
讨论再动手。
