"""SSH 多跳隧道：用系统 OpenSSH 子进程建立本地端口转发。

设计（DESIGN.md 第三节）：复用系统 ssh，天然支持多跳（ProxyJump 链）与 ~/.ssh/config。
jump_hosts 是完整跳板链，最后一跳是"落地并从其视角转发到数据库"的主机：

    ssh -N -L <本地端口>:<db_host>:<db_port> -J jump1,...,jumpN-1 jumpN

- 单跳板：ssh -N -L local:db:port bastion
- 无跳板：不建隧道，直连

每跳独立证书：命令行 `-i`/`-o` 只作用于最终目标、不作用于 `-J` 链上的跳板，因此当任一跳
指定了自己的私钥/known_hosts 时，改为生成一个临时 ssh 配置文件（`ssh -F`），每个 Host 块
带自己的 IdentityFile/UserKnownHostsFile，用 ProxyJump 串成链——这样每跳证书才真正生效。
纯遗留场景（没有任何跳板带 key）仍走 `-J`，行为与从前完全一致。

凭证一律走各自的密钥文件 / ssh-agent / ~/.ssh（BatchMode=yes，禁止交互式密码），
避免把密钥内容带进本进程；生成的 ssh 配置里也只有路径引用，绝无密钥内容。
"""

from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass

from .config import JumpHost, SshIdentity, parse_hostspec

# ssh 通用选项：转发失败即退出、保活探测、禁止交互式密码
_SSH_BASE_OPTS = [
    "-N",  # 不执行远程命令，只做端口转发
    "-o", "ExitOnForwardFailure=yes",
    "-o", "BatchMode=yes",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3",
]

_READY_TIMEOUT_S = 15.0
_READY_POLL_INTERVAL_S = 0.2


class TunnelError(Exception):
    """隧道建立失败。异常信息不得包含任何凭证内容。"""


@dataclass(frozen=True)
class ResolvedHop:
    """证书引用解析完成后的一跳：证书库名字已展开成实际路径。"""

    host: str
    user: str | None = None
    port: int | None = None
    key_path: str | None = None
    known_hosts_path: str | None = None

    def hostspec(self) -> str:
        """"user@host:port" 形式，用于遗留的 -J 链拼接。"""
        s = f"{self.user}@{self.host}" if self.user else self.host
        if self.port:
            s += f":{self.port}"
        return s

    def has_key_material(self) -> bool:
        return bool(self.key_path or self.known_hosts_path)


def _as_resolved_hop(h: "ResolvedHop | JumpHost | str") -> ResolvedHop:
    if isinstance(h, ResolvedHop):
        return h
    if isinstance(h, str):
        h = JumpHost.model_validate(parse_hostspec(h))
    return ResolvedHop(host=h.host, user=h.user, port=h.port,
                       key_path=h.key_path, known_hosts_path=h.known_hosts_path)


def resolve_jump_hosts(
    jump_hosts: list[JumpHost | str] | None,
    identities: dict[str, SshIdentity] | None = None,
) -> list[ResolvedHop]:
    """把 jump_hosts（含证书库引用）解析成 ResolvedHop 列表。

    - identity 名字优先：在证书库里查不到即报错（默认拒绝，不静默降级）。
    - 无 identity 时用内联 key_path / known_hosts_path。
    - 兼容裸字符串跳板（"user@host:port"）。
    """
    identities = identities or {}
    resolved: list[ResolvedHop] = []
    for jh in jump_hosts or []:
        if isinstance(jh, str):
            jh = JumpHost.model_validate(parse_hostspec(jh))
        host, user, port = jh.host, jh.user, jh.port
        key_path = jh.key_path
        known_hosts_path = jh.known_hosts_path
        if jh.identity:
            ident = identities.get(jh.identity)
            if ident is None:
                available = ", ".join(sorted(identities)) or "（空）"
                raise TunnelError(
                    f"跳板 {jh.label()} 引用的 SSH 配置 {jh.identity!r} 不存在，"
                    f"可用配置: {available}"
                )
            key_path = ident.key_path
            known_hosts_path = ident.known_hosts_path
            # 跳板未指定的 host/user/port 从所引用的 SSH 配置继承
            host = jh.host or ident.host
            user = jh.user or ident.user
            port = jh.port or ident.port
        if not host:
            raise TunnelError(
                f"跳板缺少 host：跳板本身与所引用的 SSH 配置 {jh.identity!r} 都未提供主机"
            )
        resolved.append(ResolvedHop(
            host=host, user=user, port=port,
            key_path=key_path, known_hosts_path=known_hosts_path,
        ))
    return resolved


def _find_free_port() -> int:
    """向内核申请一个空闲的本地端口（存在极小竞争窗口，由 ExitOnForwardFailure 兜底）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build_ssh_command(
    local_port: int,
    db_host: str,
    db_port: int,
    jump_hosts: list[str],
    ssh_options: list[str] | None = None,
) -> list[str]:
    """构造 ssh 命令 argv（遗留 -J 路径，无每跳独立证书）。纯函数，便于单测。

    jump_hosts 非空；最后一跳作为 ssh 目标，其余作为 -J 链。
    ssh_options 原样插入（如 -i key、-o UserKnownHostsFile=...），供全局凭证场景。
    """
    if not jump_hosts:
        raise ValueError("build_ssh_command 要求至少一个跳板")
    *proxy_chain, target = jump_hosts
    cmd = ["ssh", *_SSH_BASE_OPTS, *(ssh_options or []),
           "-L", f"127.0.0.1:{local_port}:{db_host}:{db_port}"]
    if proxy_chain:
        cmd += ["-J", ",".join(proxy_chain)]
    cmd.append(target)
    return cmd


_HOP_ALIAS = "dbmhop"  # 生成的 ssh 配置里给每跳起的 Host 别名前缀


def build_ssh_config(hops: list[ResolvedHop]) -> str:
    """为「每跳独立证书」生成 ssh 配置文件文本。纯函数，便于单测。

    Host * 放全局选项（对链上每跳生效，顺带修「per-hop known_hosts 要 -F」的坑）；
    每跳一个 Host dbmhopN 块带自己的 IdentityFile/UserKnownHostsFile，用 ProxyJump 串链。
    配置里只有路径引用，绝无密钥内容。
    """
    if not hops:
        raise ValueError("build_ssh_config 要求至少一个跳板")

    def _q(v: str) -> str:
        return '"' + v.replace('"', '\\"') + '"'

    lines = [
        "Host *",
        "    BatchMode yes",
        "    ExitOnForwardFailure yes",
        "    ServerAliveInterval 30",
        "    ServerAliveCountMax 3",
        "",
    ]
    for i, hop in enumerate(hops):
        lines.append(f"Host {_HOP_ALIAS}{i}")
        lines.append(f"    HostName {hop.host}")
        if hop.user:
            lines.append(f"    User {hop.user}")
        if hop.port:
            lines.append(f"    Port {hop.port}")
        if hop.key_path:
            lines.append(f"    IdentityFile {_q(hop.key_path)}")
            lines.append("    IdentitiesOnly yes")
        if hop.known_hosts_path:
            lines.append(f"    UserKnownHostsFile {_q(hop.known_hosts_path)}")
        if i > 0:
            lines.append(f"    ProxyJump {_HOP_ALIAS}{i - 1}")
        lines.append("")
    return "\n".join(lines)


class SSHTunnel:
    """一条到数据库的本地转发隧道。线程安全的 close。"""

    def __init__(
        self,
        db_host: str,
        db_port: int,
        hops: list[ResolvedHop],
        ssh_options: list[str] | None = None,
    ):
        self._db_host = db_host
        self._db_port = db_port
        # 容错：允许直接传裸字符串/JumpHost（无证书引用解析），统一成 ResolvedHop
        self._hops = [_as_resolved_hop(h) for h in hops]
        self._ssh_options = ssh_options or []
        self._proc: subprocess.Popen | None = None
        self._config_path: str | None = None
        self._lock = threading.Lock()
        self.local_port: int = 0

    def start(self) -> "SSHTunnel":
        if not self._hops:
            raise ValueError("SSHTunnel 要求至少一个跳板")
        self.local_port = _find_free_port()
        cmd = self._build_command()
        # stderr 捕获用于失败诊断；不捕获 stdout（-N 无输出）
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError as e:
            self._cleanup_config()
            raise TunnelError("未找到 ssh 命令，请确认已安装 OpenSSH 客户端") from e

        self._wait_ready()
        return self

    def _build_command(self) -> list[str]:
        """任一跳带独立证书 → 生成临时 ssh 配置文件走 `ssh -F`；否则走遗留 -J。"""
        forward = f"127.0.0.1:{self.local_port}:{self._db_host}:{self._db_port}"
        if any(h.has_key_material() for h in self._hops):
            config_text = build_ssh_config(self._hops)
            fd, path = tempfile.mkstemp(prefix="dbm-ssh-", suffix=".conf")
            try:
                os.write(fd, config_text.encode("utf-8"))
            finally:
                os.close(fd)
            os.chmod(path, 0o600)  # 虽只含路径也收紧权限
            self._config_path = path
            target_alias = f"{_HOP_ALIAS}{len(self._hops) - 1}"
            return ["ssh", "-F", path, "-N", "-L", forward,
                    *self._ssh_options, target_alias]
        # 遗留路径：无每跳证书，沿用 -J
        return build_ssh_command(
            self.local_port, self._db_host, self._db_port,
            [h.hostspec() for h in self._hops], self._ssh_options,
        )

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + _READY_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                stderr = self._drain_stderr()
                self.close()
                raise TunnelError(
                    f"隧道建立失败（跳板链 {self._describe_chain()}）：{stderr or 'ssh 进程提前退出'}"
                )
            if self._port_open():
                return
            time.sleep(_READY_POLL_INTERVAL_S)
        # 超时：必须先终止 ssh 再读 stderr——进程活着时 stderr.read() 会一直阻塞到
        # ssh 自行退出（如 DNS 解析挂起时长达数分钟），曾把探测/测试整个卡死
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        stderr = self._drain_stderr()
        self.close()
        raise TunnelError(
            f"隧道就绪超时 {_READY_TIMEOUT_S}s（跳板链 {self._describe_chain()}）：{stderr or '本地转发端口未打开'}"
        )

    def _port_open(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            return s.connect_ex(("127.0.0.1", self.local_port)) == 0

    def _drain_stderr(self) -> str:
        if self._proc is None or self._proc.stderr is None:
            return ""
        try:
            return (self._proc.stderr.read() or "").strip()
        except Exception:
            return ""

    def _describe_chain(self) -> str:
        chain = [h.hostspec() for h in self._hops]
        return " -> ".join([*chain, f"{self._db_host}:{self._db_port}"])

    def _cleanup_config(self) -> None:
        path = self._config_path
        self._config_path = None
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def close(self) -> None:
        with self._lock:
            proc = self._proc
            self._proc = None
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            if proc.stderr is not None:
                proc.stderr.close()
        self._cleanup_config()


def open_tunnel(
    db_host: str,
    db_port: int,
    jump_hosts: list[JumpHost | str],
    ssh_options: list[str] | None = None,
    identities: dict[str, SshIdentity] | None = None,
) -> SSHTunnel:
    hops = resolve_jump_hosts(jump_hosts, identities)
    return SSHTunnel(db_host, db_port, hops, ssh_options).start()
