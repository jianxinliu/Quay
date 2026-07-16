# 安全策略 / Security Policy

Quay（包名 `dbmcp`）是一个**面向数据库的安全治理工作台**——它同时给人和 AI Agent 提供
受控的数据库访问。安全是这个项目的核心卖点，因此我们认真对待每一个漏洞报告。

## 报告漏洞 / Reporting a Vulnerability

**请不要在公开 issue 中披露安全漏洞。**

- 首选：通过 GitHub 的 **Private Vulnerability Reporting**（仓库 Security → Report a
  vulnerability）私下提交。
- 备选：邮件 `shmilyljx@gmail.com`，标题请带 `[SECURITY]`。

请在报告中包含：受影响版本、复现步骤或 PoC、影响面评估、以及（如有）建议修复方向。
我们会在 **72 小时内**确认收到，并在评估后与你协商披露时间线。修复发布前请勿公开。

## 威胁模型 / Threat Model

Quay 的设计部署形态是**本地进程模式**：默认绑定 `127.0.0.1:8100`，管理后台与 MCP 端点
共用一个 ASGI 应用。安全设计围绕以下边界（详见 `DESIGN.md`）：

- **默认拒绝**：SQL 解析失败、多语句无法分类、无法判定的一律按写操作处理，进审批流。
- **拒绝—重提审批**：Agent 的写操作先被拒绝并生成审批单（含风险报告），人工在后台批准、
  Agent 带 `change_id` 重提后才放行；执行的永远是审批单里存储的 SQL（指纹校验）。
  审批单一次性核销（CAS 防并发双花）、有 TTL。**prod 环境强制审批。**
- **双账号**：日常查询走只读账号，仅审批通过的执行切换 writer 账号。
- **密钥不落明文**：配置只存 `env://` / `keyring://` 引用；密码永不出现在日志、审计记录、
  工具返回值中；Redis `CONFIG`/`ACL` 结果与命令原文中的凭证在审计与返回前脱敏。
- **连接与密钥管理不暴露为 MCP 工具**——Agent 碰不到；只有人可通过 CLI 或已认证的后台操作。
- **后台认证**：`DBM_ADMIN_TOKEN` → HMAC 签名 cookie（httponly + SameSite=Lax）。
- **本地来源校验**：管理后台校验 `Host` 白名单（防 DNS rebinding）+ 写请求校验 `Origin`
  （防跨站状态变更）。
- **prod 写二次闸门**：查询台 / Redis 控制台对生产环境的写操作，除风险确认外还需再次
  输入连接名匹配才放行。

### 不在威胁模型内 / Out of Scope

- 将服务暴露到 `127.0.0.1` 以外（公网 / LAN）——**不是设计用法**。若必须如此，请自行放在
  受信反向代理 + 网络隔离之后，并通过 `DBM_ADMIN_ALLOWED_HOSTS` 显式配置允许的 Host。
- 拥有后台 `DBM_ADMIN_TOKEN` 或本机 shell 的人被视为受信操作者。
- 底层数据库、SSH 跳板主机、操作系统 keyring 自身的安全性。

## 支持版本 / Supported Versions

项目处于活跃开发期，仅对最新发布版本提供安全修复。请始终使用最新版。

## 已知限制 / Known Limitations

安全相关的已知限制与设计取舍记录在 `DESIGN.md`。如果你认为某项限制构成实际可利用的
漏洞，欢迎按上述渠道报告。
