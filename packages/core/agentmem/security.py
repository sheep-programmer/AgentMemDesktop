"""安全边界工具。

AgentMem 是本地优先的单机应用，但**不能因此放松边界检查**：

- 服务监听在 127.0.0.1，浏览器里的任意页面都可能向它发请求；
- Space 可以从 **zip 压缩包导入**，压缩包里的 ``meta.db`` 是完全不受信的输入 ——
  攻击者能借此把任意 ``source_uri`` / ``raw_path`` 写进数据库；
- 抓取网页的功能天然能被用来探测内网。

因此凡是「用户可控的路径」与「用户可控的 URL」，都必须过这里的守卫。
"""

from __future__ import annotations

import contextlib
import ipaddress
import socket
from pathlib import Path
from typing import Final
from urllib.parse import urlparse

from .errors import ValidationError

__all__ = [
    "ALLOWED_SCHEMES",
    "MAX_REDIRECTS",
    "SENSITIVE_FIELD_NAMES",
    "ensure_within",
    "redact_sensitive",
    "resolve_public_url",
]


# --------------------------------------------------------------- 路径沙箱


def ensure_within(base: Path, target: Path) -> Path:
    """确认 ``target`` 位于 ``base`` 之内，返回解析后的真实路径。

    两边都先 ``resolve()``：既展开 ``..``，也跟随符号链接 ——
    只查字符串前缀是不够的，一个指向 ``/etc`` 的软链能轻松绕过。

    Raises:
        ValidationError: 目标越出了 ``base``。
    """
    base_resolved = base.resolve()
    try:
        target_resolved = target.resolve()
    except OSError as exc:  # 路径过长、循环软链等
        raise ValidationError("路径无法解析", detail={"path": str(target)}) from exc

    if not target_resolved.is_relative_to(base_resolved):
        # 不要把解析后的绝对路径回显给调用方，那本身就是一种信息泄漏
        raise ValidationError(
            "路径越出了允许的目录范围",
            detail={"name": target.name},
        )
    return target_resolved


# --------------------------------------------------------------- SSRF 守卫

#: 只允许这两种协议。``file://`` / ``gopher://`` 之类一律拒绝。
ALLOWED_SCHEMES: Final = frozenset({"http", "https"})

#: 单次抓取允许的最大重定向跳数。
MAX_REDIRECTS: Final = 5


def _reject_private(host: str) -> list[str]:
    """解析主机名并确认所有解析结果都是公网地址，返回这些地址。

    必须检查**全部** DNS 记录：只看第一条的话，
    一个同时解析到公网与 127.0.0.1 的域名就能绕过（DNS rebinding 的变体）。
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValidationError("域名无法解析", detail={"host": host}) from exc

    addresses: list[str] = []
    for info in infos:
        raw = info[4][0]
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:  # pragma: no cover - getaddrinfo 不该返回非法地址
            continue
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local  # 含 169.254.169.254 云元数据
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            raise ValidationError(
                "禁止访问内网、回环或保留地址",
                detail={"host": host, "resolved": str(address)},
            )
        addresses.append(str(address))

    if not addresses:
        raise ValidationError("域名未解析到任何可用地址", detail={"host": host})
    return addresses


def resolve_public_url(url: str) -> str:
    """校验 URL 指向公网，通过则原样返回。

    **每一次重定向之后都要重新调用本函数**：只校验首个 URL 的话，
    一个返回 302 到 ``http://169.254.169.254/`` 的公网地址就能把守卫绕干净。

    Raises:
        ValidationError: 协议不允许、主机缺失，或解析到了非公网地址。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise ValidationError(
            "只允许 http / https 协议",
            detail={"scheme": parsed.scheme or "(空)"},
        )
    if not parsed.hostname:
        raise ValidationError("URL 缺少主机名", detail={"url": url})

    _reject_private(parsed.hostname)
    return url


# --------------------------------------------------------------- 错误脱敏

#: 这些字段名出现在校验错误里时，其原始输入值必须被打码。
#: FastAPI 的 ``RequestValidationError.errors()`` 默认带 ``input``，
#: 会把用户刚提交的明文密钥原样回显在 422 响应里。
SENSITIVE_FIELD_NAMES: Final = frozenset(
    {"api_key", "apikey", "token", "secret", "password", "authorization"}
)

_REDACTED: Final = "***已隐藏***"


def redact_sensitive(value: object, *, _key: str | None = None) -> object:
    """递归打码敏感字段的值，用于错误响应与日志。

    判定依据是**字段名**而非值的内容：值的形态千变万化，名字是可靠的信号。
    校验错误里字段名可能出现在 ``loc`` 元组中，因此也一并检查。
    """
    if _key is not None and _key.lower() in SENSITIVE_FIELD_NAMES:
        return _REDACTED

    if isinstance(value, dict):
        result: dict[object, object] = {}
        # 校验错误形如 {"loc": ["body", "api_key"], "input": "sk-真实密钥"}，
        # 敏感与否要看 loc 的末位，不是看 "input" 这个键名本身。
        loc = value.get("loc")
        loc_is_sensitive = isinstance(loc, (list, tuple)) and any(
            isinstance(part, str) and part.lower() in SENSITIVE_FIELD_NAMES for part in loc
        )
        for key, item in value.items():
            if loc_is_sensitive and key == "input":
                result[key] = _REDACTED
            else:
                result[key] = redact_sensitive(item, _key=key if isinstance(key, str) else None)
        return result

    if isinstance(value, (list, tuple)):
        return [redact_sensitive(item) for item in value]

    return value


def locate_raw_file(raw_dir: Path, stored: str | None) -> Path | None:
    """按库里记下的路径找到本 Space ``raw/`` 目录下的原始文件，找不到返回 ``None``。

    库里的路径不可靠，两个原因：

    - **形态不一**：粘贴 / 网页存的是相对路径（``data/spaces/…``，依赖启动目录），
      上传文件存的是绝对路径。换个目录启动，或把备份还原到另一台机器，两种都会失效——
      文件明明就在 ``raw/`` 里，重新解析和下载却都报「原始文件不存在」。
    - **不可信**：Space 能从 zip 导入，``meta.db`` 由外部提供，路径可以被写成
      ``/etc/passwd``。解析时照着它读，就会把任意文件读进知识库。

    所以先认原路径（必须落在 ``raw/`` 内），不行再按文件名到 ``raw/`` 里找。
    """
    if not stored:
        return None
    candidates: list[Path] = []
    with contextlib.suppress(ValidationError):
        candidates.append(ensure_within(raw_dir, Path(stored)))
    candidates.append(raw_dir / Path(stored).name)
    for candidate in candidates:
        try:
            resolved = ensure_within(raw_dir, candidate)
        except ValidationError:
            continue
        if resolved.is_file():
            return resolved
    return None
