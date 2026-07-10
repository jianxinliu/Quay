"""连接管理：管理后台增删改连接的业务逻辑（与 HTML 渲染解耦，便于单测）。

密钥安全：页面输入的密码写入系统 keyring，配置文件只存 keyring:// 引用（不落明文）。
写连接后热加载配置并回收该连接的旧引擎/隧道，无需重启。
"""

from __future__ import annotations

from pathlib import Path

from .config import AppConfig, ConnectionConfig, Policy, ProjectConfig, WriterAccount, save_config
from .secrets import SecretResolveError, delete_keyring_secret, store_keyring_secret


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
        jump_hosts: list[str],
        ssh_key_path: str | None,
        ssh_options_extra: list[str],
        max_rows: int,
        mask_columns: list[str],
        force_privileged: bool = False,
    ) -> None:
        if not project or not connection:
            raise ConnectionAdminError("项目名与连接名不能为空")

        existing = self.config.projects.get(project, ProjectConfig()).connections.get(connection)

        # SSH 参数
        ssh_options = list(ssh_options_extra)
        if ssh_key_path:
            validate_ssh_key_path(ssh_key_path)
            ssh_options = ["-i", ssh_key_path, *ssh_options]

        # 保存前自动权限校验（mysql/pg，未勾选强制时）：连库探测账号权限，
        # 超级用户 / 有写权限 / 连不上 → 阻止保存。在写 keyring 前用明文探测。
        if engine in ("mysql", "postgres") and not force_privileged:
            self._check_account_privilege(
                engine, environment, host, port, database, user,
                password, existing, jump_hosts, ssh_options, max_rows,
            )

        # 密码：留空表示沿用旧引用；新连接必须给（sqlite/无 auth redis 除外）
        password_ref = self._resolve_password_ref(
            project, connection, "reader", password, existing.password if existing else None
        )
        writer_account = self._resolve_writer(
            project, connection, writer_user, writer_password, existing
        )

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
                jump_hosts=jump_hosts,
                ssh_options=ssh_options,
                policy=Policy(max_rows=max_rows, mask_columns=mask_columns),
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
        # 清理 keyring（仅清 keyring:// 引用；env:// 等外部管理的不动）
        delete_keyring_secret(removed.password or "")
        if removed.writer is not None:
            delete_keyring_secret(removed.writer.password)
        save_config(self.config, self.config_path)

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

        res = probe_connection(probe_cfg, None)
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
                return store_keyring_secret(_keyring_account(project, connection, role), new_plain)
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
