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
    ) -> None:
        if not project or not connection:
            raise ConnectionAdminError("项目名与连接名不能为空")

        existing = self.config.projects.get(project, ProjectConfig()).connections.get(connection)

        # 密码：留空表示沿用旧引用；新连接必须给（sqlite/无 auth redis 除外）
        password_ref = self._resolve_password_ref(
            project, connection, "reader", password, existing.password if existing else None
        )
        writer_account = self._resolve_writer(
            project, connection, writer_user, writer_password, existing
        )

        # SSH 参数
        ssh_options = list(ssh_options_extra)
        if ssh_key_path:
            validate_ssh_key_path(ssh_key_path)
            ssh_options = ["-i", ssh_key_path, *ssh_options]

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
