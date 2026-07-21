"""连接配置模型与加载。

配置文件为 YAML，结构见 DESIGN.md 第四节。密码字段只存引用（env:// 等），
由 secrets.resolve_secret 在建立连接时解析，绝不缓存到模型之外。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

Engine = Literal["mysql", "postgres", "sqlite", "redis", "clickhouse"]
Environment = Literal["local", "dev", "staging", "prod"]

DEFAULT_MAX_ROWS = 1000
DEFAULT_STATEMENT_TIMEOUT_S = 30
# writer 账号的读写超时：大 DELETE/UPDATE 会长时间无返回，用 reader 的 30s socket 超时
# 会触发 pymysql 2013「Lost connection ... read operation timed out」。writer 独立放大，
# 配合手动取消（KILL QUERY）兜住跑飞的写。
DEFAULT_WRITE_TIMEOUT_S = 600


class SshIdentity(BaseModel):
    """一条可复用的 SSH 配置：主机/用户/端口/私钥/known_hosts。

    只存路径引用，绝不存密钥文件内容。host/user/port 可选——跳板引用本配置时，
    跳板处未填的字段从这里继承（向后兼容：旧的只含 key_path 的配置，host/user/port 为 None，
    此时跳板必须自己写 host）。
    """

    host: str | None = None
    user: str | None = None
    port: int | None = None
    key_path: str
    known_hosts_path: str | None = None


def parse_hostspec(spec: str) -> dict[str, Any]:
    """把 "user@host:port" 拆成 {host, user?, port?}。纯函数，便于单测。

    只解析 user/host/port 三段；host 为空视为非法。用于兼容旧的 jump_hosts 字符串形式，
    以及界面里用户直接粘贴一个 hostspec 的情况。
    """
    rest = spec.strip()
    if not rest:
        raise ValueError("跳板 host 不能为空")
    user: str | None = None
    port: int | None = None
    if "@" in rest:
        user, rest = rest.split("@", 1)
        user = user.strip() or None
    # 用 rsplit 兼容 IPv6 不在此支持（保持与 ssh -J 的简单 host:port 语义一致）
    if ":" in rest:
        host_part, port_part = rest.rsplit(":", 1)
        if port_part.strip().isdigit():
            rest, port = host_part, int(port_part)
    host = rest.strip()
    if not host:
        raise ValueError(f"跳板地址非法: {spec!r}")
    out: dict[str, Any] = {"host": host}
    if user:
        out["user"] = user
    if port is not None:
        out["port"] = port
    return out


class JumpHost(BaseModel):
    """一跳跳板。证书两种来源：identity（引用 ssh_identities 的名字）或内联 key_path。

    兼容旧配置：裸字符串 "user@host:port" 会被 before-validator 解析成本模型。
    """

    host: str | None = None          # 可空：仅引用 SSH 配置时，host 从配置继承
    user: str | None = None
    port: int | None = None
    identity: str | None = None      # 引用 AppConfig.ssh_identities 的键名
    key_path: str | None = None      # 或内联私钥路径（不引用证书库时）
    known_hosts_path: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> Any:
        if isinstance(v, str):
            return parse_hostspec(v)
        return v

    @model_validator(mode="after")
    def _need_host_or_identity(self) -> "JumpHost":
        if not self.host and not self.identity:
            raise ValueError("跳板必须填 host，或引用一条 SSH 配置（identity）")
        return self

    def label(self) -> str:
        """用于列表/日志展示的一行文本，如 "alice@bastion:22"。不含任何密钥内容。"""
        base = self.host or (f"@{self.identity}" if self.identity else "?")
        s = f"{self.user}@{base}" if self.user else base
        if self.port:
            s += f":{self.port}"
        return s


class Policy(BaseModel):
    max_rows: int = DEFAULT_MAX_ROWS
    # reader 的语句/socket 读超时（秒）：MySQL max_execution_time + socket read_timeout；PG statement_timeout
    statement_timeout_s: int = DEFAULT_STATEMENT_TIMEOUT_S
    # writer 的 socket 读写超时（秒）：MySQL read/write_timeout；PG writer 的 statement_timeout
    write_timeout_s: int = DEFAULT_WRITE_TIMEOUT_S
    auto_approve_low_risk_write: bool = False
    # 单元格最大字符数：超长 TEXT/BLOB 截断，防止撑爆 agent 上下文
    max_cell_chars: int = 4096
    # 给 agent 返回结果的字符预算（≈token×4）；None = 用全局设置兜底。行数上限之外的第二道
    # 硬限：宽表 200 行也可能几万 token，按序列化后大小截断才真正盯住上下文成本
    agent_max_result_chars: int | None = None
    # 敏感字段脱敏：内置模式（password/token/secret 等）+ 自定义列名（不区分大小写）
    mask_default_patterns: bool = True
    mask_columns: list[str] = Field(default_factory=list)
    # elicitation 快捷审批：None = 按环境自动（local/dev 开、staging/prod 关）
    elicitation_approval: bool | None = None


class WriterAccount(BaseModel):
    user: str
    password: str  # 密钥引用，如 env://XXX


class ConnectionConfig(BaseModel):
    engine: Engine
    environment: Environment = "local"
    host: str | None = None
    port: int | None = None
    database: str | None = None
    user: str | None = None
    password: str | None = None  # 密钥引用
    writer: WriterAccount | None = None
    jump_hosts: list[JumpHost] = Field(default_factory=list)
    # 额外 ssh 参数（如 -i /path/key、-o UserKnownHostsFile=...），原样插入 ssh 命令
    ssh_options: list[str] = Field(default_factory=list)
    policy: Policy = Field(default_factory=Policy)

    @model_validator(mode="after")
    def _check_required_by_engine(self) -> "ConnectionConfig":
        if self.engine == "sqlite":
            if not self.database:
                raise ValueError("sqlite 连接必须提供 database（文件路径或 :memory:）")
        elif self.engine == "redis":
            # Redis 可以无账号（本地无 auth）或仅 requirepass（无 user）
            if self.host is None:
                raise ValueError("redis 连接缺少必填字段: host")
        elif self.engine == "clickhouse":
            # ClickHouse 需要 host + user；password 可空（default 用户常无密码），database 可空（默认 default 库）
            missing = [f for f in ("host", "user") if getattr(self, f) is None]
            if missing:
                raise ValueError(f"clickhouse 连接缺少必填字段: {', '.join(missing)}")
        else:
            missing = [f for f in ("host", "user", "password") if getattr(self, f) is None]
            if missing:
                raise ValueError(f"{self.engine} 连接缺少必填字段: {', '.join(missing)}")
        return self

    @property
    def elicitation_enabled(self) -> bool:
        """elicitation 快捷审批开关：显式配置优先，否则按环境（local/dev 开）。"""
        if self.policy.elicitation_approval is not None:
            return self.policy.elicitation_approval
        return self.environment in ("local", "dev")


class ProjectConfig(BaseModel):
    connections: dict[str, ConnectionConfig] = Field(default_factory=dict)


class AppConfig(BaseModel):
    projects: dict[str, ProjectConfig] = Field(default_factory=dict)
    # 可复用的 SSH 证书库（名字 → 证书）；只存路径引用。连接的 jump_hosts 用名字引用。
    ssh_identities: dict[str, SshIdentity] = Field(default_factory=dict)

    def get_connection(self, project: str, connection: str) -> ConnectionConfig:
        proj = self.projects.get(project)
        if proj is None:
            available = ", ".join(sorted(self.projects)) or "（无）"
            raise KeyError(f"项目 {project!r} 不存在，可用项目: {available}")
        conn = proj.connections.get(connection)
        if conn is None:
            available = ", ".join(sorted(proj.connections)) or "（无）"
            raise KeyError(f"项目 {project!r} 下连接 {connection!r} 不存在，可用连接: {available}")
        return conn


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return AppConfig.model_validate(raw)


def save_config(config: AppConfig, path: str | Path) -> None:
    """把配置写回 YAML（原子替换）。

    只序列化非默认字段保持文件精简；password 字段存的是引用（keyring:// 等），
    非明文，因此写回安全。注意：会丢失原文件的注释——UI 管理后文件即由 UI 维护。
    """
    path = Path(path)
    data = config.model_dump(mode="json", exclude_defaults=True, exclude_none=True)
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
