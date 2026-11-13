import pytest

from dbmcp.secrets import SecretResolveError, resolve_secret


def test_env_ref(monkeypatch):
    monkeypatch.setenv("DBM_TEST_PW", "s3cret")
    assert resolve_secret("env://DBM_TEST_PW") == "s3cret"


def test_env_ref_missing(monkeypatch):
    monkeypatch.delenv("DBM_TEST_MISSING", raising=False)
    with pytest.raises(SecretResolveError, match="DBM_TEST_MISSING"):
        resolve_secret("env://DBM_TEST_MISSING")


def test_plain_ref():
    assert resolve_secret("plain://hello") == "hello"


def test_empty_ref_means_no_password():
    """省略密码是合法配置（ClickHouse 的 default 用户、无 auth 的本地实例）。

    「没配」必须解析成「没有密码」，不能走「未知的密钥引用格式」那条分支——
    否则用户看到的是配置格式错误，真实原因是这个连接本来就不需要密码。
    """
    assert resolve_secret("") == ""
    assert resolve_secret(None) == ""


def test_bare_string_rejected():
    # 裸字符串（疑似明文密码直接写进配置）必须报错
    with pytest.raises(SecretResolveError, match="未知的密钥引用格式"):
        resolve_secret("my-plaintext-password")


def test_keyring_bad_format():
    with pytest.raises(SecretResolveError, match="keyring://service/account"):
        resolve_secret("keyring://only-service")


def test_error_message_never_leaks_value(monkeypatch):
    """异常信息中不能出现密钥内容。"""
    monkeypatch.setenv("DBM_TEST_PW", "super-secret-value")
    try:
        resolve_secret("env://DBM_TEST_PW_TYPO")
    except SecretResolveError as e:
        assert "super-secret-value" not in str(e)
