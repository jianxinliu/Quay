"""连接探测：测试连通性 + 判定账号权限（是否越权/超级用户），供测试按钮与保存校验用。

针对表单里尚未保存的配置临时建连，不入池。权限判定是启发式的：
- MySQL：解析 SHOW GRANTS，含 ALL PRIVILEGES 或 INSERT/UPDATE/DELETE/DDL 关键字即判有写权限
- Postgres：pg_roles.rolsuper / rolcreatedb / rolcreaterole + role_table_grants 的写权限
- SQLite/Redis：无账号权限模型，只测连通，不判越权
复杂的库级/表级授权组合可能识别不全——判定结果供人参考，不作唯一依据。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import text

from . import engines
from .config import ConnectionConfig, Policy, SshIdentity
from .tunnel import SSHTunnel, TunnelError, open_tunnel

# 各引擎的超级用户常见默认名
_SUPERUSER_NAMES = {"mysql": {"root"}, "postgres": {"postgres"}}
_MYSQL_WRITE_KEYWORDS = ("ALL PRIVILEGES", "INSERT", "UPDATE", "DELETE",
                         "CREATE", "DROP", "ALTER", "TRUNCATE", "GRANT OPTION")


@dataclass
class ProbeResult:
    ok: bool
    message: str = ""
    version: str | None = None
    has_write: bool | None = None      # 账号是否握有写权限（None=未判定，如 sqlite/redis）
    is_superuser: bool | None = None   # 是否 root/超级用户
    warnings: list[str] = field(default_factory=list)

    @property
    def privileged(self) -> bool:
        """高权限账号：超级用户，或有写权限。"""
        return bool(self.is_superuser) or bool(self.has_write)


def probe_connection(
    cfg: ConnectionConfig, password_plain: str | None,
    identities: dict[str, SshIdentity] | None = None,
) -> ProbeResult:
    """临时建 reader 连接，测连通 + 判定权限。password_plain 为明文（表单未保存）。"""
    probe_cfg = cfg.model_copy(update={
        "password": f"plain://{password_plain}" if password_plain is not None else cfg.password,
        "policy": Policy(statement_timeout_s=min(cfg.policy.statement_timeout_s, 10)),
    })
    if cfg.engine == "redis":
        from . import redis_engine
        try:
            pooled = redis_engine.build_probe_client(probe_cfg)  # 内部已 PING
            pooled.dispose()
        except Exception as e:
            return ProbeResult(ok=False, message=f"连接失败：{_clean(e)}")
        return ProbeResult(ok=True, message="连接成功（Redis 无账号权限模型）")

    try:
        pooled = engines.build_probe_engine(probe_cfg, role="reader", identities=identities)
    except Exception as e:
        return ProbeResult(ok=False, message=f"连接失败：{_clean(e)}")

    try:
        with pooled.engine.connect() as conn:
            if cfg.engine == "sqlite":
                return ProbeResult(ok=True, message="连接成功（SQLite 无账号权限模型）")
            if cfg.engine == "mysql":
                return _probe_mysql(conn, cfg.user or "")
            if cfg.engine == "postgres":
                return _probe_postgres(conn, cfg.user or "")
            return ProbeResult(ok=True, message="连接成功")
    except Exception as e:
        return ProbeResult(ok=False, message=f"连接失败：{_clean(e)}")
    finally:
        pooled.dispose()


def analyze_mysql_grants(grants: list[str], user: str) -> tuple[bool, bool]:
    """从 SHOW GRANTS 结果判定 (has_write, is_superuser)。纯函数，便于单测。"""
    grants_text = " ".join(grants).upper()
    has_write = any(k in grants_text for k in _MYSQL_WRITE_KEYWORDS)
    is_super = (
        user.lower() in _SUPERUSER_NAMES["mysql"]
        or "ALL PRIVILEGES ON *.*" in grants_text
        or ("GRANT OPTION" in grants_text and "ON *.*" in grants_text)
    )
    return has_write, is_super


def _probe_mysql(conn, user: str) -> ProbeResult:  # noqa: ANN001
    version = conn.execute(text("SELECT version()")).scalar()
    grants = [str(r[0]) for r in conn.execute(text("SHOW GRANTS")).fetchall()]
    has_write, is_super = analyze_mysql_grants(grants, user)
    return ProbeResult(ok=True, message="连接成功", version=str(version),
                       has_write=has_write, is_superuser=is_super)


def _probe_postgres(conn, user: str) -> ProbeResult:  # noqa: ANN001
    version = conn.execute(text("SHOW server_version")).scalar()
    row = conn.execute(text(
        "SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = current_user"
    )).fetchone()
    is_super = bool(row and row[0])
    can_create = bool(row and (row[1] or row[2]))
    table_write = conn.execute(text(
        "SELECT count(*) FROM information_schema.role_table_grants"
        " WHERE grantee = current_user AND privilege_type IN ('INSERT','UPDATE','DELETE')"
    )).scalar() or 0
    has_write = is_super or can_create or table_write > 0
    is_super = is_super or user.lower() in _SUPERUSER_NAMES["postgres"]
    return ProbeResult(ok=True, message="连接成功", version=str(version),
                       has_write=has_write, is_superuser=is_super)


def probe_ssh(
    cfg: ConnectionConfig, identities: dict[str, SshIdentity] | None = None
) -> ProbeResult:
    """只建 SSH 隧道验证跳板链（不连数据库）。"""
    if not cfg.jump_hosts:
        return ProbeResult(ok=False, message="该连接未配置 SSH 跳板")
    default_port = {"mysql": 3306, "postgres": 5432, "redis": 6379}.get(cfg.engine, 0)
    chain = " → ".join(h.label() for h in cfg.jump_hosts)
    tunnel: SSHTunnel | None = None
    try:
        tunnel = open_tunnel(cfg.host or "", cfg.port or default_port,
                             cfg.jump_hosts, cfg.ssh_options, identities)
        return ProbeResult(ok=True, message=f"SSH 隧道建立成功（跳板链 {chain}）")
    except TunnelError as e:
        return ProbeResult(ok=False, message=_clean(e))
    finally:
        if tunnel is not None:
            tunnel.close()


def _clean(e: Exception) -> str:
    """精简异常信息，取首行，避免堆栈/密钥引用泄露到 UI。"""
    return str(e).splitlines()[0][:300]
