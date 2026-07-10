# db-manage-mcp

给所有 agent 提供数据库访问的 MCP 服务：按项目管理连接与账密、SSH 多层跳板、SQL 审计与人工授权、操作审计与管理后台。完整设计见 `DESIGN.md`（改动核心流程前先读它）。

## 技术栈（已确认，勿擅自更换）

- Python 3.12 + FastMCP（官方 MCP SDK）+ FastAPI（MCP 与管理后台共用一个 ASGI 应用）
- sqlglot 做 SQL AST 解析与审计；SQLAlchemy Core（不用 ORM）+ redis-py
- SQLite 存审计/审批单/元数据缓存；Jinja2 + HTMX 做管理后台
- SSH 多跳复用系统 OpenSSH（`ssh -J jump1,jump2`），不自己实现隧道协议
- 部署形态：本地 Docker 常驻 daemon（streamable HTTP），stdio 作兼容模式

## 安全设计红线（实现时必须遵守）

1. **默认拒绝**：SQL 解析失败、多语句、无法分类的一律按写操作处理，进审批流。
2. **密钥不落明文**：配置文件只存 `env://` / `keyring://` / `agefile://` 引用；密码永不出现在日志、审计记录、工具返回值中。Docker 内不可用 macOS Keychain，容器部署走 env/加密文件。
3. **拒绝—重提 + change_id 放行**：写操作先被明确拒绝并生成审批单（含风险报告），人在后台批准后 agent 带 change_id 重提才放行；**执行的永远是审批单里存储的 SQL**，重提文本只作指纹校验（不一致即拒），指纹匹配仅作无 id 时的兜底。审批单一次性核销、有 TTL（30 分钟）。prod 环境强制审批。
4. **双账号**：日常查询走只读账号，仅审批通过的执行切换 writer 账号。
5. **连接与密钥管理只走 CLI**（人操作），不暴露为 MCP 工具。
6. 通知遵循"安静即正常"：不主动推送，审批挂起由 agent 在会话中告知用户。

## 约定

- 每条 SQL 必须落审计记录：agent（clientInfo + session）、时间、连接、SQL 原文与指纹、风险等级、结果（行数/耗时/状态）、审批信息。
- 查询默认加 LIMIT（1000）与语句超时（30s），可按连接配置。
- Redis 走命令分类模型：读命令直通；KEYS / FLUSHDB / FLUSHALL / CONFIG 属 CRITICAL 需审批。

## 开发

```bash
uv sync --extra keyring    # 安装依赖
uv run pytest              # 全量测试（改动后必须全过）
uv run dbm                 # HTTP daemon（127.0.0.1:8100）
uv run dbm --stdio         # stdio 模式
docker compose up -d --build
```

- 代码在 `src/dbmcp/`，分层：`server.py`（MCP 接口）→ `service.py`（核心逻辑，与传输解耦、可直接单测）→ `engines.py` / `audit/`（分类器 + 审计日志）。新增工具时逻辑写在 service 层，server 层只做注册和 ToolError 转换。

## 经验教训

- **包名是 `dbmcp`，不是 `dbm`**：`dbm` 是 Python 标准库模块，会被 stdlib 遮蔽导致 import 全挂。CLI 命令仍叫 `dbm`。
- **本机测本地服务要绕过代理**：这台机器 shell 有 SOCKS 代理环境变量，不绕过会把 127.0.0.1 请求发进代理得到 502。curl 用 `--noproxy '*'`；fastmcp Client（底层 httpx）用 `env NO_PROXY='*' no_proxy='*'`（单纯 `-u ALL_PROXY` 不够，httpx 还会读其他代理变量，NO_PROXY 最稳）。
- **写 sqlglot 相关逻辑前先跑实验脚本验证解析行为**：已验证的坑——PG 方言 `EXPLAIN`/`SHOW` 退化为不透明 `Command` 节点需特判；`WITH x AS (INSERT ...) SELECT` 顶层是 `Select`，必须遍历整棵树找写节点。
- fastmcp 3.x 装的是 `fastmcp` + `fastmcp-slim`，其依赖 `uncalled-for` 是正常的依赖注入库（已核实非投毒包）。
- **MySQL 会话设置的多条语句不能逗号拼接**：`SET SESSION max_execution_time=X, SESSION TRANSACTION READ ONLY` 是非法语法（1064）——`SET TRANSACTION READ ONLY` 是独立语句、不是变量赋值。必须用 connect 事件监听器逐条 execute。此 bug 只有真实 MySQL e2e 才暴露，SQLite 单测发现不了；已抽 `mysql_session_statements()` 纯函数 + 回归测试（test_engines.py）。**教训：DB 方言相关代码，SQLite 单测不够，必须对目标 DB 跑真实 e2e。**
- 真实 MySQL 9.5 e2e 已验证通过：reader 只读防线报 1792、writer 双账号写入、information_schema 行数估算、索引命中风险判定、完整拒绝—重提审批闭环、管理后台批准。
- **ProxyJump 的两个坑（多跳 e2e 踩出）**：① 命令行 `-o` 选项只作用于最终目标、不传递给跳板连接——自定义密钥/known_hosts 必须用 `-F 配置文件`（对链上每跳生效），对应 `ssh_options: ["-F", path]`；② sshd_config 是**首个匹配生效**，往文件末尾 append `AllowTcpForwarding yes` 无效，容器里用 `sshd -o` 命令行覆盖。多跳验证脚本：`scripts/e2e_ssh_multihop.sh`（两级 sshd 容器链到本地 MySQL，凭证全程隔离在 /tmp）。

## 模块地图（src/dbmcp/）

- `server.py` MCP 工具注册 → `service.py` 核心逻辑（可单测）
- `engines.py` SQLAlchemy 适配 + 引擎池（托管 SSH 隧道、reader/writer 双角色、空闲回收）
- `tunnel.py` 系统 OpenSSH 多跳隧道；`metadata.py` 元数据缓存（TTL）
- `audit/classify.py` 只读判定 + 指纹；`audit/risk.py` 风险评估；`audit/log.py` 操作审计
- `approvals.py` 审批单存储与生命周期；`admin.py` 管理后台（Starlette custom_route，服务端渲染）
- `redis_engine.py` Redis 池；`audit/redis_rules.py` 命令分类；`masking.py` 脱敏；`__main__.py` serve/approvals/approve/reject 子命令
- elicitation 快捷审批在 `server.py::_maybe_elicit_approval`：审批单先创建（审计完整），elicitation 只是把批准动作搬进会话；客户端不支持时异常被吞、自然回退审批单流程

## 当前状态

- [x] 设计定稿（DESIGN.md）
- [x] M1 骨架：daemon（Docker）+ SecretProvider + MySQL/PG/SQLite 只读 query + schema + 操作审计
- [x] M2 连接：SSH 多跳隧道 + 引擎池（隧道托管/空闲回收）+ 元数据缓存
- [x] M3 管控：风险审计引擎 + 拒绝—重提审批流（change_id 放行、writer 双账号）+ 管理后台
- [x] M4 增强：elicitation 快捷审批（local/dev 默认开，审批单始终留痕）+ Redis 适配（命令分类 + 同一套审批流，真实 Redis 7 e2e 通过）+ 敏感字段脱敏 + CLI 审批兜底（dbm approvals/approve/reject）。130 个测试全过。
- [ ] goInception 可选集成：**有意未做**——需要跑起 goInception 实例才能联调，不写无法验证的集成代码；接入点预留在 audit/risk.py（MySQL 深度审核可作为 assess 的增强数据源）

## 真实集成验证状态

- ✅ MySQL 9.5（本机）、PostgreSQL 17（Docker）、Redis 7（Docker）、SSH 两跳隧道（scripts/e2e_ssh_multihop.sh）均已真实 e2e 通过。
- writer 账号的"仅审批通过才使用"依赖 config 正确配置独立账号；sqlite 无账号概念，测试里 writer 复用同库。
- 空闲回收 + 保留期清理由 `service.start_housekeeping()` 驱动（serve 时启动，60s 一轮，保留期默认 30 天可由 `--retention-days`/`DBM_RETENTION_DAYS` 配）。
