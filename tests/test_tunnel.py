"""SSH 隧道命令构造与失败路径测试。

真实多跳隧道需要活的 bastion，无法在单测环境验证，仅测纯逻辑与快速失败。
"""

import pytest

from dbmcp.config import JumpHost, SshIdentity
from dbmcp.tunnel import (
    ResolvedHop,
    SSHTunnel,
    TunnelError,
    _find_free_port,
    build_ssh_command,
    build_ssh_config,
    resolve_jump_hosts,
)


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


class TestResolveJumpHosts:
    def test_identity_expanded(self):
        jhs = [JumpHost(host="b1", user="alice", identity="id1"),
               JumpHost(host="b2", key_path="/inline/k2")]
        idents = {"id1": SshIdentity(key_path="/k1", known_hosts_path="/kh1")}
        hops = resolve_jump_hosts(jhs, idents)
        assert hops[0].key_path == "/k1" and hops[0].known_hosts_path == "/kh1"
        assert hops[1].key_path == "/inline/k2"  # 内联直接用

    def test_unknown_identity_rejected(self):
        with pytest.raises(TunnelError, match="不存在"):
            resolve_jump_hosts([JumpHost(host="b", identity="ghost")], {})

    def test_legacy_string_no_key(self):
        hops = resolve_jump_hosts(["alice@b1:22", "b2"], {})
        assert [h.hostspec() for h in hops] == ["alice@b1:22", "b2"]
        assert not any(h.has_key_material() for h in hops)


class TestBuildSshConfig:
    def test_per_hop_identity_and_proxyjump(self):
        hops = [ResolvedHop(host="b1", user="alice", key_path="/k1"),
                ResolvedHop(host="b2", port=2222, key_path="/k2", known_hosts_path="/kh2")]
        text = build_ssh_config(hops)
        assert "Host dbmhop0" in text and "Host dbmhop1" in text
        assert 'IdentityFile "/k1"' in text and 'IdentityFile "/k2"' in text
        assert 'UserKnownHostsFile "/kh2"' in text
        assert "ProxyJump dbmhop0" in text  # 第二跳经第一跳
        assert "BatchMode yes" in text  # 全局块对每跳生效

    def test_legacy_path_uses_dash_j_not_config(self):
        # 无任一跳带 key → SSHTunnel 走遗留 -J，不生成配置文件
        t = SSHTunnel("db", 3306, [ResolvedHop(host="b1"), ResolvedHop(host="b2")])
        cmd = t._build_command()
        assert "-F" not in cmd and "-J" in cmd
        assert t._config_path is None

    def test_key_path_uses_config_file(self):
        t = SSHTunnel("db", 3306, [ResolvedHop(host="b1", key_path="/k1")])
        cmd = t._build_command()
        assert "-F" in cmd and t._config_path is not None
        import os
        os.unlink(t._config_path)  # 清理临时文件
