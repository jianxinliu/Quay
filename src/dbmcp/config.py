"""连接配置模型与加载。

配置文件为 YAML，结构见 DESIGN.md 第四节。密码字段只存引用（env:// 等），
由 secrets.resolve_secret 在建立连接时解析，绝不缓存到模型之外。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

Engine = Literal["mysql", "postgres", "sqlite", "redis"]
Environment = Literal["local", "dev", "staging", "prod"]

DEFAULT_MAX_ROWS = 1000
DEFAULT_STATEMENT_TIMEOUT_S = 30
# writer 账号的读写超时：大 DELETE/UPDATE 会长时间无返回，用 reader 的 30s socket 超时
# 会触发 pymysql 2013「Lost connection ... read operation timed out」。writer 独立放大，
# 配合手动取消（KILL QUERY）兜住跑飞的写。
DEFAULT_WRITE_TIMEOUT_S = 600


class Policy(BaseModel):
    max_rows: int = DEFAULT_MAX_ROWS
    # reader 的语句/socket 读超时（秒）：MySQL max_execution_time + socket read_timeout；PG statement_timeout
    statement_timeout_s: int = DEFAULT_STATEMENT_TIMEOUT_S
    # writer 的 socket 读写超时（秒）：MySQL read/write_timeout；PG writer 的 statement_timeout
    write_timeout_s: int = DEFAULT_WRITE_TIMEOUT_S
    auto_approve_low_risk_write: bool = False
    # 单元格最大字符数：超长 TEXT/BLOB 截断，防止撑爆 agent 上下文
    max_cell_chars: int = 4096
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
    jump_hosts: list[str] = Field(default_factory=list)
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
