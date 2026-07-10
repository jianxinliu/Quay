"""SecretProvider：把配置文件中的密钥引用解析为真实密钥。

支持的引用格式（必须显式带 scheme，裸字符串一律报错，防止明文误入配置）：

- ``env://VAR_NAME``               从环境变量读取（Docker 部署推荐）
- ``keyring://service/account``    从系统钥匙串读取（裸机运行时用，需安装 keyring extra）
- ``plain://literal``              字面量密码（仅限 local/dev 调试，不推荐）
"""

from __future__ import annotations

import os


class SecretResolveError(Exception):
    """密钥引用无法解析。异常信息中不得包含密钥内容。"""


def resolve_secret(ref: str) -> str:
    if ref.startswith("env://"):
        var = ref.removeprefix("env://")
        value = os.environ.get(var)
        if value is None:
            raise SecretResolveError(f"环境变量 {var} 未设置（引用: {ref}）")
        return value

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
