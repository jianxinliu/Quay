"""SSH 多跳隧道：用系统 OpenSSH 子进程建立本地端口转发。

设计（DESIGN.md 第三节）：复用系统 ssh，天然支持多跳（ProxyJump 链）与 ~/.ssh/config。
jump_hosts 是完整跳板链，最后一跳是"落地并从其视角转发到数据库"的主机：

    ssh -N -L <本地端口>:<db_host>:<db_port> -J jump1,...,jumpN-1 jumpN

- 单跳板：ssh -N -L local:db:port bastion
- 无跳板：不建隧道，直连

凭证一律走 ssh-agent / ~/.ssh 密钥（BatchMode=yes，禁止交互式密码），
避免把密钥内容带进本进程。
"""

from __future__ import annotations

import socket
import subprocess
import threading
import time

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
    """构造 ssh 命令 argv。纯函数，便于单测。

    jump_hosts 非空；最后一跳作为 ssh 目标，其余作为 -J 链。
    ssh_options 原样插入（如 -i key、-o UserKnownHostsFile=...），供非默认凭证场景。
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


class SSHTunnel:
    """一条到数据库的本地转发隧道。线程安全的 close。"""

    def __init__(
        self,
        db_host: str,
        db_port: int,
        jump_hosts: list[str],
        ssh_options: list[str] | None = None,
    ):
        self._db_host = db_host
        self._db_port = db_port
        self._jump_hosts = jump_hosts
        self._ssh_options = ssh_options or []
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self.local_port: int = 0

    def start(self) -> "SSHTunnel":
        self.local_port = _find_free_port()
        cmd = build_ssh_command(
            self.local_port, self._db_host, self._db_port, self._jump_hosts, self._ssh_options
        )
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
            raise TunnelError("未找到 ssh 命令，请确认已安装 OpenSSH 客户端") from e

        self._wait_ready()
        return self

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
        return " -> ".join([*self._jump_hosts, f"{self._db_host}:{self._db_port}"])

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def close(self) -> None:
        with self._lock:
            proc = self._proc
            self._proc = None
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        if proc.stderr is not None:
            proc.stderr.close()


def open_tunnel(
    db_host: str, db_port: int, jump_hosts: list[str], ssh_options: list[str] | None = None
) -> SSHTunnel:
    return SSHTunnel(db_host, db_port, jump_hosts, ssh_options).start()
