"""SSH 隧道命令构造与失败路径测试。

真实多跳隧道需要活的 bastion，无法在单测环境验证，仅测纯逻辑与快速失败。
"""

import pytest

from dbmcp.tunnel import SSHTunnel, TunnelError, _find_free_port, build_ssh_command


class TestBuildCommand:
    def test_single_jump(self):
        cmd = build_ssh_command(15000, "db.internal", 3306, ["bastion"])
        assert cmd[0] == "ssh"
        assert "-L" in cmd
        assert cmd[cmd.index("-L") + 1] == "127.0.0.1:15000:db.internal:3306"
        assert "-J" not in cmd  # 单跳板不需要 ProxyJump
        assert cmd[-1] == "bastion"

    def test_multi_hop(self):
        cmd = build_ssh_command(15000, "db.internal", 5432, ["b1", "b2", "b3"])
        # 最后一跳是 ssh 目标，其余进 -J 链
        assert cmd[cmd.index("-J") + 1] == "b1,b2"
        assert cmd[-1] == "b3"
        assert cmd[cmd.index("-L") + 1] == "127.0.0.1:15000:db.internal:5432"

    def test_batch_mode_no_interactive_password(self):
        cmd = build_ssh_command(15000, "db", 3306, ["b"])
        joined = " ".join(cmd)
        assert "BatchMode=yes" in joined
        assert "ExitOnForwardFailure=yes" in joined

    def test_empty_jump_hosts_rejected(self):
        with pytest.raises(ValueError, match="至少一个跳板"):
            build_ssh_command(15000, "db", 3306, [])


class TestFreePort:
    def test_returns_bindable_port(self):
        port = _find_free_port()
        assert 1024 < port < 65536


class TestFailFast:
    def test_unreachable_jump_fails_quickly(self):
        # 指向一个不可达的跳板，应在就绪超时内抛 TunnelError 而非挂起
        tunnel = SSHTunnel("10.255.255.1", 3306, ["127.0.0.1:1"])
        with pytest.raises(TunnelError):
            tunnel.start()
        assert not tunnel.is_alive()
