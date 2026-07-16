"""测试会话级配置。

Starlette 的 TestClient 默认以 `Host: testserver` 发请求；管理后台新增的
Host/Origin 校验（防 DNS rebinding / 跨站写，见 admin.py `_local_request_ok`）
会把非本机 Host 挡成 403。测试里把 `testserver` 显式加入允许的 Host 白名单，
使校验逻辑在测试环境放行——生产默认白名单仍只含本机回环名，不受影响。
"""

import os

os.environ["DBM_ADMIN_ALLOWED_HOSTS"] = "testserver"
