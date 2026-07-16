"""测试会话级配置。

Starlette 的 TestClient 默认以 `Host: testserver` 发请求；管理后台新增的
Host/Origin 校验（防 DNS rebinding / 跨站写，见 admin.py `_local_request_ok`）
会把非本机 Host 挡成 403。测试里把 `testserver` 显式加入允许的 Host 白名单，
使校验逻辑在测试环境放行——生产默认白名单仍只含本机回环名，不受影响。
"""

import os

os.environ["DBM_ADMIN_ALLOWED_HOSTS"] = "testserver"


# 用内存 keyring 后端，让测试与真实系统钥匙串隔离，且行为跨平台一致：
#   - 本地（macOS）：不再把测试密码写进真实钥匙串（卫生）。
#   - CI（headless Linux/Docker）：没有系统钥匙串后端，否则连接保存写密码会 400 失败。
# 仅在装了 keyring extra 时生效（未装则相关测试本就走别的路径）。
try:
    import keyring
    from keyring.backend import KeyringBackend

    class _MemoryKeyring(KeyringBackend):
        priority = 1  # type: ignore[assignment]
        _store: dict = {}

        def get_password(self, service, username):
            return self._store.get((service, username))

        def set_password(self, service, username, password):
            self._store[(service, username)] = password

        def delete_password(self, service, username):
            self._store.pop((service, username), None)

    keyring.set_keyring(_MemoryKeyring())
except Exception:  # noqa: BLE001 - keyring 未安装/不可用时静默跳过
    pass
