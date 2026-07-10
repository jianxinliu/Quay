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
- **本机 curl 测试要加 `--noproxy '*'`**：这台机器 shell 有 SOCKS 代理环境变量，不绕过会把 127.0.0.1 请求发进代理得到 502；起本地服务进程时也建议 `env -u ALL_PROXY ...` 清掉代理变量。
- **写 sqlglot 相关逻辑前先跑实验脚本验证解析行为**：已验证的坑——PG 方言 `EXPLAIN`/`SHOW` 退化为不透明 `Command` 节点需特判；`WITH x AS (INSERT ...) SELECT` 顶层是 `Select`，必须遍历整棵树找写节点。
- fastmcp 3.x 装的是 `fastmcp` + `fastmcp-slim`，其依赖 `uncalled-for` 是正常的依赖注入库（已核实非投毒包）。

## 当前状态

- [x] 设计定稿（DESIGN.md）
- [x] M1 骨架：daemon（Docker 验证通过）+ 配置/SecretProvider + MySQL/PG/SQLite 只读 query + schema 工具 + 操作审计（52 个测试全过）
- [ ] M2 连接：SSH 多跳 + 元数据缓存（schema/索引/行数）
- [ ] M3 管控：审计引擎（风险报告）+ 拒绝—重提审批流 + 管理后台
- [ ] M4 增强：elicitation 审批 + Redis + goInception（可选）+ 脱敏
