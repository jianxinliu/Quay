"""连接管理：管理后台增删改连接的业务逻辑（与 HTML 渲染解耦，便于单测）。

密钥安全：页面输入的密码写入系统 keyring，配置文件只存 keyring:// 引用（不落明文）。
写连接后热加载配置并回收该连接的旧引擎/隧道，无需重启。
"""

from __future__ import annotations

from pathlib import Path

from .config import (
    AppConfig,
    ConnectionConfig,
    JumpHost,
    Policy,
    ProjectConfig,
    SshIdentity,
    WriterAccount,
    save_config,
)
from .secrets import SecretResolveError, delete_secret, store_secret


class ConnectionAdminError(Exception):
    """连接管理操作失败（校验不通过、keyring 不可用等）。message 面向管理员。"""


def _friendly_validation_error(e: ValueError) -> str:
    """把 pydantic 的冗长校验错误提炼成一句人话。"""
    errors = getattr(e, "errors", None)
    if callable(errors):
        msgs = []
        for err in e.errors():
            msg = str(err.get("msg", "")).removeprefix("Value error, ").strip()
            if msg:
                msgs.append(msg)
        if msgs:
            return "；".join(dict.fromkeys(msgs))  # 去重保序
    return str(e).splitlines()[0]


def _keyring_account(project: str, connection: str, role: str) -> str:
    # keyring account 不能含 '/'
    safe = f"{project}--{connection}".replace("/", "_")
    return f"{safe}--{role}" if role == "writer" else safe


def validate_ssh_key_path(path: str) -> None:
    """校验 SSH key 文件存在、可读、权限不过宽。"""
    p = Path(path).expanduser()
    if not p.exists():
        raise ConnectionAdminError(f"SSH key 文件不存在: {path}")
    if not p.is_file():
        raise ConnectionAdminError(f"SSH key 路径不是文件: {path}")
    try:
        p.read_bytes()
    except OSError as e:
        raise ConnectionAdminError(f"SSH key 文件不可读: {path}（{e}）") from e
    mode = p.stat().st_mode & 0o077
    if mode:
        raise ConnectionAdminError(
            f"SSH key 权限过宽（{oct(p.stat().st_mode & 0o777)}），"
            f"ssh 会拒绝使用，请 chmod 600 {path}"
        )


def identity_referers(config: AppConfig, name: str) -> list[str]:
    """列出引用了该证书的连接（"project/connection" 形式）。"""
    out: list[str] = []
    for pname, proj in config.projects.items():
        for cname, conn in proj.connections.items():
            if any(h.identity == name for h in conn.jump_hosts):
                out.append(f"{pname}/{cname}")
    return sorted(out)


def validate_known_hosts_path(path: str) -> None:
    """校验 known_hosts 文件存在且可读（无权限严格要求，仅是公钥指纹）。"""
    p = Path(path).expanduser()
    if not p.exists():
        raise ConnectionAdminError(f"known_hosts 文件不存在: {path}")
    if not p.is_file():
        raise ConnectionAdminError(f"known_hosts 路径不是文件: {path}")
    try:
        p.read_bytes()
    except OSError as e:
        raise ConnectionAdminError(f"known_hosts 文件不可读: {path}（{e}）") from e


def _build_jump_hosts(
    jump_hosts: list, identities: dict[str, SshIdentity]
) -> list[JumpHost]:
    """把表单/调用方传入的跳板（dict 或裸字符串）构造成 JumpHost 并校验证书引用。

    - identity 引用：必须在证书库里存在。
    - 内联 key_path / known_hosts_path：校验文件存在、权限（key 走 600 检查）。
    """
    built: list[JumpHost] = []
    for i, raw in enumerate(jump_hosts, start=1):
        try:
            jh = JumpHost.model_validate(raw)
        except ValueError as e:
            raise ConnectionAdminError(f"第 {i} 跳配置非法：{_friendly_validation_error(e)}") from e
        if jh.identity:
            if jh.identity not in identities:
                available = ", ".join(sorted(identities)) or "（空）"
                raise ConnectionAdminError(
                    f"第 {i} 跳（{jh.label()}）引用的 SSH 证书 {jh.identity!r} 不存在，"
                    f"可用证书: {available}"
                )
        else:
            if jh.key_path:
                validate_ssh_key_path(jh.key_path)
            if jh.known_hosts_path:
                validate_known_hosts_path(jh.known_hosts_path)
        built.append(jh)
    return built


class ConnectionManager:
    """对 AppConfig 做增删改并持久化，负责密钥落 keyring。"""

    def __init__(self, config: AppConfig, config_path: str | Path):
        self.config = config
        self.config_path = Path(config_path)

    def upsert(
        self,
        project: str,
        connection: str,
        *,
        engine: str,
        environment: str,
        host: str | None,
        port: int | None,
        database: str | None,
        user: str | None,
        password: str | None,           # 明文，None 表示不改（编辑时留空）
        writer_user: str | None,
        writer_password: str | None,
        jump_hosts: list,               # 每项 dict{host,user?,port?,identity?,key_path?} 或裸字符串
        ssh_options_extra: list[str],
        max_rows: int,
        mask_columns: list[str],
        force_privileged: bool = False,
        statement_timeout_s: int | None = None,
        write_timeout_s: int | None = None,
    ) -> None:
        if not project or not connection:
            raise ConnectionAdminError("项目名与连接名不能为空")

        existing = self.config.projects.get(project, ProjectConfig()).connections.get(connection)

        # SSH 跳板：构造 JumpHost（每跳可带自己的证书引用/内联 key）并校验
        hops = _build_jump_hosts(jump_hosts, self.config.ssh_identities)
        ssh_options = list(ssh_options_extra)

        # 保存前自动权限校验（mysql/pg，未勾选强制时）：连库探测账号权限，
        # 超级用户 / 有写权限 / 连不上 → 阻止保存。在写 keyring 前用明文探测。
        if engine in ("mysql", "postgres") and not force_privileged:
            self._check_account_privilege(
                engine, environment, host, port, database, user,
                password, existing, hops, ssh_options, max_rows,
            )

        # 密码：留空表示沿用旧引用；新连接必须给（sqlite/无 auth redis 除外）
        password_ref = self._resolve_password_ref(
            project, connection, "reader", password, existing.password if existing else None
        )
        writer_account = self._resolve_writer(
            project, connection, writer_user, writer_password, existing
        )

        # policy：以旧值为基准增量覆盖，避免编辑连接时把未在表单里的字段（脱敏/审批开关等）
        # 重置回默认值；读/写超时留空则沿用旧值（新连接用默认）。
        policy_update: dict = {"max_rows": max_rows, "mask_columns": mask_columns}
        if statement_timeout_s is not None:
            policy_update["statement_timeout_s"] = statement_timeout_s
        if write_timeout_s is not None:
            policy_update["write_timeout_s"] = write_timeout_s
        base_policy = existing.policy if existing else Policy()
        policy = base_policy.model_copy(update=policy_update)

        # 构造并校验（ConnectionConfig 的 validator 会检查引擎必填字段）
        try:
            conn_cfg = ConnectionConfig(
                engine=engine,
                environment=environment,
                host=host or None,
                port=port,
                database=database or None,
                user=user or None,
                password=password_ref,
                writer=writer_account,
                jump_hosts=hops,
                ssh_options=ssh_options,
                policy=policy,
            )
        except ValueError as e:
            raise ConnectionAdminError(_friendly_validation_error(e)) from e

        self.config.projects.setdefault(project, ProjectConfig()).connections[connection] = conn_cfg
        save_config(self.config, self.config_path)

    def delete(self, project: str, connection: str) -> None:
        proj = self.config.projects.get(project)
        if proj is None or connection not in proj.connections:
            raise ConnectionAdminError(f"连接 {project}/{connection} 不存在")
        removed = proj.connections.pop(connection)
        if not proj.connections:
            self.config.projects.pop(project)
        # 清理密钥（清 keyring:// / file:// 引用；env:// 等外部管理的不动）
        delete_secret(removed.password or "")
        if removed.writer is not None:
            delete_secret(removed.writer.password)
        save_config(self.config, self.config_path)

    # ---------- SSH 证书库（只存路径引用，绝不存密钥内容）----------

    def upsert_identity(
        self, name: str, key_path: str, known_hosts_path: str | None = None,
        host: str | None = None, user: str | None = None, port: str | int | None = None,
    ) -> None:
        """新增/修改一条可复用的 SSH 配置。校验路径可用后原地写入并持久化。

        host/user/port 可选：跳板引用本配置时继承这些字段（跳板处可覆盖）。
        """
        name = (name or "").strip()
        if not name:
            raise ConnectionAdminError("配置名不能为空")
        key_path = (key_path or "").strip()
        if not key_path:
            raise ConnectionAdminError("私钥路径不能为空")
        validate_ssh_key_path(key_path)
        known_hosts_path = (known_hosts_path or "").strip() or None
        if known_hosts_path:
            validate_known_hosts_path(known_hosts_path)
        port_num: int | None = None
        if str(port or "").strip():
            try:
                port_num = int(port)
            except (TypeError, ValueError) as e:
                raise ConnectionAdminError(f"端口必须是数字：{port!r}") from e
        # 原地改 dict（引擎池持有同一引用，即时可见）
        self.config.ssh_identities[name] = SshIdentity(
            host=(host or "").strip() or None, user=(user or "").strip() or None,
            port=port_num, key_path=key_path, known_hosts_path=known_hosts_path,
        )
        save_config(self.config, self.config_path)

    def delete_identity(self, name: str) -> None:
        """删除证书。被任何连接的跳板引用时拒删（列出引用者）。"""
        if name not in self.config.ssh_identities:
            raise ConnectionAdminError(f"SSH 配置 {name!r} 不存在")
        referers = self.identity_referers(name)
        if referers:
            raise ConnectionAdminError(
                f"SSH 配置 {name!r} 正被以下连接引用，不能删除：{'、'.join(referers)}"
            )
        self.config.ssh_identities.pop(name)
        save_config(self.config, self.config_path)

    def identity_referers(self, name: str) -> list[str]:
        """列出引用了该证书的连接（"project/connection" 形式）。"""
        return identity_referers(self.config, name)

    def _check_account_privilege(
        self, engine, environment, host, port, database, user,  # noqa: ANN001
        password, existing, jump_hosts, ssh_options, max_rows,  # noqa: ANN001
    ) -> None:
        from .probe import probe_connection

        eff_pw = f"plain://{password}" if password else (existing.password if existing else None)
        if eff_pw is None:
            raise ConnectionAdminError("缺少数据库密码，无法建连校验账号权限")
        try:
            probe_cfg = ConnectionConfig(
                engine=engine, environment=environment, host=host or None, port=port,
                database=database or None, user=user or None, password=eff_pw,
                jump_hosts=jump_hosts, ssh_options=ssh_options,
                policy=Policy(max_rows=max_rows),
            )
        except ValueError as e:
            raise ConnectionAdminError(_friendly_validation_error(e)) from e

        res = probe_connection(probe_cfg, None, self.config.ssh_identities)
        if not res.ok:
            raise ConnectionAdminError(
                f"无法连接以校验账号权限：{res.message}。"
                "确认配置无误后，可勾选“强制使用高权限账号”跳过校验并保存。"
            )
        if res.privileged:
            reasons = []
            if res.is_superuser:
                reasons.append("是超级用户/root 账号")
            if res.has_write:
                reasons.append("拥有写权限（INSERT/UPDATE/DELETE/DDL）")
            raise ConnectionAdminError(
                f"该连接账号{'、'.join(reasons)}；只读连接应使用最小权限的只读账号。"
                "确需使用请勾选“强制使用高权限账号”。"
            )

    # ---------- 内部 ----------

    def _resolve_password_ref(
        self, project: str, connection: str, role: str,
        new_plain: str | None, existing_ref: str | None,
    ) -> str | None:
        if new_plain:
            try:
                return store_secret(_keyring_account(project, connection, role), new_plain)
            except SecretResolveError as e:
                raise ConnectionAdminError(str(e)) from e
        return existing_ref  # 留空 → 沿用旧引用（可能为 None）

    def _resolve_writer(
        self, project: str, connection: str,
        writer_user: str | None, writer_password: str | None,
        existing: ConnectionConfig | None,
    ) -> WriterAccount | None:
        if not writer_user:
            return None
        existing_writer_ref = existing.writer.password if existing and existing.writer else None
        ref = self._resolve_password_ref(
            project, connection, "writer", writer_password, existing_writer_ref
        )
        if ref is None:
            raise ConnectionAdminError("配置了 writer 用户但未提供 writer 密码")
        return WriterAccount(user=writer_user, password=ref)
