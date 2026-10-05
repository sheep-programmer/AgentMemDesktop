"""本地 API 的来源守卫：挡住跨站请求与 DNS 重绑定。

后端没有登录鉴权——它只监听本机，默认信任本机上的调用方。可「本机」不只是你自己：
你用浏览器打开的任何网页，都能向 127.0.0.1:8765 发请求。实测两条路都走得通：

- **跨站简单请求**：不带请求体的 POST（重建索引、开始进化）不触发 CORS 预检，
  恶意网页读不到响应，但副作用照样发生——进化一次要调几十次模型、花你的额度。
- **DNS 重绑定**：攻击者把自己的域名解析到 127.0.0.1，浏览器就把它当同源，
  连 DELETE 都能发、响应也读得到。实测用 ``Host: rebind.evil.example`` 删除空间返回 200。

所以两道检查：

1. ``Host`` 必须是本机名（localhost / 127.0.0.1 / ::1）或配置的监听地址——重绑定时
   ``Host`` 是攻击者的域名，过不了这一关。监听在 0.0.0.0 / :: 时是用户有意开给局域网，
   主机名无从预知，这一关不做。
2. 会改数据的请求（POST / PUT / PATCH / DELETE）若带 ``Origin``，必须来自本机或
   CORS 白名单。命令行、curl、脚本不带 ``Origin``，不受影响。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import Request, Response

from agentmem.config import Settings
from agentmem.errors import ForbiddenOriginError

LOCAL_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})
#: 测试客户端的 Host：Starlette TestClient 默认 ``testserver``，httpx 用例多用 ``http://test``。
#: 都不是可注册的公网域名（``test`` 是 RFC 6761 保留名），重绑定用不上它们。
TEST_CLIENT_HOSTS = frozenset({"testserver", "test"})
WILDCARD_BINDS = frozenset({"0.0.0.0", "::", ""})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _hostname(value: str) -> str:
    """从 ``Host`` 头或 URL 里取主机名（去端口、去 IPv6 方括号、转小写）。"""
    netloc = urlsplit(value).netloc if "://" in value else value
    if netloc.startswith("["):
        return netloc[1 : netloc.find("]")].lower()
    return netloc.rsplit(":", 1)[0].lower() if netloc.count(":") == 1 else netloc.lower()


class LocalGuard:
    """按设置算好白名单，逐个请求判定。"""

    def __init__(self, settings: Settings) -> None:
        self.check_host = settings.host not in WILDCARD_BINDS
        self.allowed_hosts = LOCAL_HOSTNAMES | TEST_CLIENT_HOSTS | {settings.host.lower()}
        self.allowed_origins = {origin.rstrip("/") for origin in settings.cors_origin_list}

    def host_allowed(self, host: str | None) -> bool:
        if not self.check_host or not host:
            return True
        return _hostname(host) in self.allowed_hosts

    def origin_allowed(self, origin: str | None) -> bool:
        # 没有 Origin：命令行 / 脚本 / 同源的 GET 表单，照常放行
        if not origin or origin == "null":
            return origin is None
        if origin.rstrip("/") in self.allowed_origins:
            return True
        return _hostname(origin) in LOCAL_HOSTNAMES

    def reject_reason(self, request: Request) -> str | None:
        if not self.host_allowed(request.headers.get("host")):
            return "请求的主机名不是本机，已拒绝（防 DNS 重绑定）"
        if request.method not in SAFE_METHODS and not self.origin_allowed(
            request.headers.get("origin")
        ):
            return "请求来自其他网页，已拒绝（防跨站请求）"
        return None


def install_local_guard(
    app: object, settings: Settings, render_error: Callable[[ForbiddenOriginError], Response]
) -> None:
    """把守卫挂成最外层中间件之一：先于路由、先于任何写库动作判定。"""
    from fastapi import FastAPI

    assert isinstance(app, FastAPI)
    guard = LocalGuard(settings)

    @app.middleware("http")
    async def local_guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        reason = guard.reject_reason(request)
        if reason is not None:
            return render_error(
                ForbiddenOriginError(
                    reason,
                    detail={
                        "host": request.headers.get("host"),
                        "origin": request.headers.get("origin"),
                    },
                )
            )
        return await call_next(request)
