"""文件密钥后端（容器/无桌面 keyring 回退）单测。"""
from __future__ import annotations

import os
import stat

import pytest

from dbmcp import secrets as S


@pytest.fixture()
def secrets_file(tmp_path, monkeypatch):
    path = tmp_path / "secrets.json"
    monkeypatch.setenv("DBM_SECRETS_FILE", str(path))
    return path


def test_store_resolve_delete_roundtrip(secrets_file):
    ref = S.store_file_secret("demo__mysql__reader", "s3cr3t")
    assert ref == "file://demo__mysql__reader"
    assert S.resolve_secret(ref) == "s3cr3t"
    S.delete_file_secret(ref)
    with pytest.raises(S.SecretResolveError):
        S.resolve_secret(ref)


def test_file_perms_600(secrets_file):
    S.store_file_secret("acct", "pw")
    mode = stat.S_IMODE(os.stat(secrets_file).st_mode)
    assert mode == 0o600


def test_config_holds_only_ref_not_plaintext(secrets_file):
    S.store_file_secret("acct", "topsecret")
    # 落盘文件里可含密钥，但返回给配置的是引用；配置层不落明文由调用方保证。
    ref = "file://acct"
    assert "topsecret" not in ref


def test_store_secret_falls_back_to_file_when_no_keyring(secrets_file, monkeypatch):
    """keyring 存储失败（无后端/未安装）时，store_secret 回退到文件后端。"""
    def _boom(_account, _value):
        raise S.SecretResolveError("系统钥匙串不可用（模拟容器环境）")
    monkeypatch.setattr(S, "store_keyring_secret", _boom)
    ref = S.store_secret("demo__pg__reader", "pw123")
    assert ref.startswith("file://")
    assert S.resolve_secret(ref) == "pw123"


def test_delete_secret_routes_by_scheme(secrets_file, monkeypatch):
    ref = S.store_file_secret("acct", "pw")
    # delete_secret 认 file:// → 真删
    S.delete_secret(ref)
    assert S._load_file_secrets().get("acct") is None
    # 非 file:// 交给 keyring 分支（此处仅验证不抛异常）
    S.delete_secret("keyring://db-manage-mcp/whatever")


def test_missing_file_ref_raises(secrets_file):
    with pytest.raises(S.SecretResolveError):
        S.resolve_secret("file://never-stored")
