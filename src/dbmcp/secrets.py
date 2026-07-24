"""SecretProvider：把配置文件中的密钥引用解析为真实密钥。

支持的引用格式（必须显式带 scheme，裸字符串一律报错，防止明文误入配置）：

- ``env://VAR_NAME``               从环境变量读取（Docker 部署推荐）
- ``keyring://service/account``    从系统钥匙串读取（裸机运行时用，需安装 keyring extra）
- ``file://account``               从 600 权限的本地密钥文件读取（容器/无桌面环境 keyring
                                   不可用时，后台管理连接的密码回退到此，见 store_secret）
- ``plain://literal``              字面量密码（仅限 local/dev 调试，不推荐）

统一入口 ``store_secret`` / ``delete_secret`` 会优先用钥匙串、无后端时回退文件后端；
配置文件里始终只存引用、不落明文。
"""

from __future__ import annotations

import json
import os
import stat


KEYRING_SERVICE = "db-manage-mcp"


# ---------- 文件密钥后端（容器/无桌面环境的 keyring 回退） ----------
# 存到一个 600 权限的 JSON 文件（默认 ~/.config/db-manage-mcp/secrets.json，容器里
# 因 $HOME=/lzcapp/var 落在持久化卷）。配置文件里仍只存 file:// 引用、不落明文；
# 安全级别与设计中「Docker 推荐的 env://（600 env 文件）」一致。

def _secrets_file() -> str:
    return os.environ.get("DBM_SECRETS_FILE") or os.path.expanduser(
        "~/.config/db-manage-mcp/secrets.json")


def _load_file_secrets() -> dict[str, str]:
    path = _secrets_file()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_file_secrets(data: dict[str, str]) -> None:
    path = _secrets_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 600，仅属主可读写
    os.replace(tmp, path)


def store_file_secret(account: str, value: str) -> str:
    """把密码写入 600 权限的本地密钥文件，返回可写进配置的 file:// 引用。"""
    if "/" in account:
        raise SecretResolveError(f"file account 不能包含 '/': {account!r}")
    data = _load_file_secrets()
    data[account] = value
    _save_file_secrets(data)
    return f"file://{account}"


def delete_file_secret(ref: str) -> None:
    """删除 file:// 引用对应的密钥（非 file:// 引用则忽略）。"""
    if not ref.startswith("file://"):
        return
    account = ref.removeprefix("file://")
    data = _load_file_secrets()
    if account in data:
        del data[account]
        _save_file_secrets(data)


def store_secret(account: str, value: str) -> str:
    """存储密码并返回配置引用：优先系统钥匙串；无可用后端（容器/无桌面）时
    回退到 600 权限的本地密钥文件，保证 Docker 部署下也能用后台管理连接。"""
    try:
        return store_keyring_secret(account, value)
    except SecretResolveError:
        return store_file_secret(account, value)


def delete_secret(ref: str) -> None:
    """按引用类型删除对应密钥（keyring:// 或 file://）。"""
    if ref and ref.startswith("file://"):
        delete_file_secret(ref)
    else:
        delete_keyring_secret(ref)


class SecretResolveError(Exception):
    """密钥引用无法解析。异常信息中不得包含密钥内容。"""


def store_keyring_secret(account: str, value: str) -> str:
    """把密码写入系统钥匙串，返回可写进配置的 keyring:// 引用。

    account 不能含 '/'（resolve_secret 按首个 '/' 切分 service/account）。
    """
    if "/" in account:
        raise SecretResolveError(f"keyring account 不能包含 '/': {account!r}")
    try:
        import keyring  # noqa: PLC0415
    except ImportError as e:
        raise SecretResolveError(
            "未安装 keyring，无法安全存储密码：pip install 'db-manage-mcp[keyring]'"
        ) from e
    try:
        keyring.set_password(KEYRING_SERVICE, account, value)
    except Exception as e:
        # Docker/无桌面环境下 keyring 无可用后端（无 Keychain / D-Bus secret service）
        raise SecretResolveError(
            "系统钥匙串不可用（常见于 Docker/无桌面环境），无法通过页面安全存储密码。"
            "请改用配置文件的 env:// 引用注入密码，或在有钥匙串的本地进程模式下管理连接。"
        ) from e
    return f"keyring://{KEYRING_SERVICE}/{account}"


def delete_keyring_secret(ref: str) -> None:
    """删除 keyring 引用对应的密钥（非 keyring:// 引用则忽略）。"""
    if not ref.startswith("keyring://"):
        return
    service, _, account = ref.removeprefix("keyring://").partition("/")
    if not account:
        return
    try:
        import keyring  # noqa: PLC0415
        import keyring.errors  # noqa: PLC0415
    except ImportError:
        return
    try:
        keyring.delete_password(service, account)
    except keyring.errors.PasswordDeleteError:
        pass


def resolve_secret(ref: str) -> str:
    if ref.startswith("env://"):
        var = ref.removeprefix("env://")
        value = os.environ.get(var)
        if value is None:
            raise SecretResolveError(f"环境变量 {var} 未设置（引用: {ref}）")
        return value

    if ref.startswith("file://"):
        account = ref.removeprefix("file://")
        data = _load_file_secrets()
        if account not in data:
            raise SecretResolveError(f"本地密钥文件中找不到 {account}")
        return data[account]

    if ref.startswith("keyring://"):
        path = ref.removeprefix("keyring://")
        service, sep, account = path.partition("/")
        if not sep or not service or not account:
            raise SecretResolveError(f"keyring 引用格式应为 keyring://service/account，实际: {ref}")
        try:
            import keyring  # noqa: PLC0415 可选依赖，按需导入
        except ImportError as e:
            raise SecretResolveError(
                "未安装 keyring，请安装可选依赖: pip install 'db-manage-mcp[keyring]'"
            ) from e
        value = keyring.get_password(service, account)
        if value is None:
            raise SecretResolveError(f"钥匙串中找不到 {service}/{account}")
        return value

    if ref.startswith("plain://"):
        return ref.removeprefix("plain://")

    raise SecretResolveError(
        f"未知的密钥引用格式: {ref!r}，必须为 env:// / keyring:// / plain:// 之一"
    )
