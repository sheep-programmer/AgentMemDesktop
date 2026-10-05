"""安全边界回归测试。

这里每一条都对应一个**实际被利用过**的漏洞（Phase 1 交叉审计发现），
不是假想威胁。删掉任何一条之前，先确认对应的攻击路径已经不存在。
"""

from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from agentmem.errors import ValidationError
from agentmem.security import ensure_within, redact_sensitive, resolve_public_url
from agentmem.store.vectors import sql_literal

# ------------------------------------------------------------------ 路径沙箱


def test_ensure_within_allows_inside(tmp_path: Path) -> None:
    base = tmp_path / "raw"
    base.mkdir()
    target = base / "note.md"
    target.write_text("x", encoding="utf-8")
    assert ensure_within(base, target) == target.resolve()


@pytest.mark.parametrize(
    "hostile",
    [
        "/etc/passwd",  # 绝对路径
        "../../../../etc/passwd",  # 相对遍历
        "raw/../../secret.txt",  # 中段回退
    ],
)
def test_ensure_within_rejects_escape(tmp_path: Path, hostile: str) -> None:
    """Space 可以从 zip 导入，meta.db 里的路径字段完全不可信。"""
    base = tmp_path / "raw"
    base.mkdir()
    with pytest.raises(ValidationError):
        ensure_within(base, Path(hostile) if hostile.startswith("/") else base / hostile)


def test_ensure_within_rejects_symlink_escape(tmp_path: Path) -> None:
    """只比对字符串前缀是不够的——软链能指到沙箱外面去。"""
    base = tmp_path / "raw"
    base.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = base / "innocent.txt"
    link.symlink_to(outside)

    with pytest.raises(ValidationError):
        ensure_within(base, link)


def test_ensure_within_does_not_leak_resolved_path(tmp_path: Path) -> None:
    """报错详情里不能回显解析后的绝对路径，那本身就是信息泄漏。"""
    base = tmp_path / "raw"
    base.mkdir()
    with pytest.raises(ValidationError) as excinfo:
        ensure_within(base, Path("/etc/passwd"))
    assert "/etc" not in str(excinfo.value.detail)


# ------------------------------------------------------------------ SSRF


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/admin",  # 回环
        "http://localhost:8080/admin",
        "http://169.254.169.254/latest/meta-data/",  # 云厂商元数据
        "http://10.0.0.5/internal",  # 私有网段
        "http://192.168.1.1/router",
        "http://[::1]:8080/",  # IPv6 回环
    ],
)
def test_resolve_public_url_rejects_internal(url: str) -> None:
    with pytest.raises(ValidationError, match=r"内网|回环|保留"):
        resolve_public_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "gopher://127.0.0.1:11211/_stats",
        "ftp://example.com/x",
        "data:text/html,<script>",
    ],
)
def test_resolve_public_url_rejects_bad_scheme(url: str) -> None:
    with pytest.raises(ValidationError, match=r"http"):
        resolve_public_url(url)


def test_resolve_public_url_rejects_missing_host() -> None:
    with pytest.raises(ValidationError):
        resolve_public_url("http:///nohost")


class _RedirectHandler(BaseHTTPRequestHandler):
    """把访问者 302 到回环地址——模拟绕过守卫的经典手法。"""

    def do_GET(self) -> None:
        self.send_response(302)
        self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
        self.end_headers()

    def log_message(self, *args: object) -> None:  # 静默
        return


@pytest.fixture
def redirect_server() -> object:
    server = HTTPServer(("127.0.0.1", 0), _RedirectHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()


async def test_fetch_url_blocks_loopback_target(redirect_server: str) -> None:
    """抓取入口本身就该拒绝回环地址。"""
    from agentmem.ingest.parse import fetch_url_markdown

    with pytest.raises(ValidationError, match=r"内网|回环|保留"):
        await fetch_url_markdown(redirect_server)


def test_getaddrinfo_all_records_are_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    """只看第一条 DNS 记录是不够的：同时解析到公网与回环的域名必须被拒。"""

    def fake_getaddrinfo(host: str, *args: object, **kwargs: object) -> list[object]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ValidationError, match=r"内网|回环|保留"):
        resolve_public_url("http://dual-homed.example.com/")


# ------------------------------------------------------------------ 错误脱敏


def test_redact_validation_errors_hides_api_key() -> None:
    """Pydantic v2 的 errors() 带 input，会把刚提交的明文密钥回显在 422 里。"""
    errors = [
        {
            "type": "string_too_short",
            "loc": ["body", "api_key"],
            "msg": "too short",
            "input": "sk-真实密钥-不该出现",
        },
        {
            "type": "missing",
            "loc": ["body", "model"],
            "msg": "field required",
            "input": "qwen3:14b",
        },
    ]
    redacted = redact_sensitive(errors)
    dumped = str(redacted)

    assert "sk-真实密钥-不该出现" not in dumped
    assert "***已隐藏***" in dumped
    # 非敏感字段要原样保留，否则错误信息就没用了
    assert "qwen3:14b" in dumped
    assert "field required" in dumped


@pytest.mark.parametrize("field", ["api_key", "API_KEY", "token", "password", "secret"])
def test_redact_covers_sensitive_names(field: str) -> None:
    assert redact_sensitive({field: "机密"}) == {field: "***已隐藏***"}


def test_redact_is_recursive() -> None:
    payload = {"outer": [{"inner": {"api_key": "机密"}}]}
    assert "机密" not in str(redact_sensitive(payload))


# ------------------------------------------------------------------ 过滤表达式转义


def test_sql_literal_escapes_quotes() -> None:
    """LanceDB 的 where 没有参数绑定，值必须自己转义。"""
    assert sql_literal("abc") == "'abc'"
    assert sql_literal("it's") == "'it''s'"
    # 经典注入载荷不能逃出字面量
    assert sql_literal("x' OR '1'='1") == "'x'' OR ''1''=''1'"


# ------------------------------------------------------ 导入压缩包的路径沙箱


async def _tampered_archive(tmp_path: Path, space_id: str) -> bytes:
    """造一个「导出后把 meta.db 里的 space id 改掉」的压缩包。"""
    import io
    import sqlite3
    import zipfile

    from agentmem.config import get_settings
    from agentmem.space.manager import SpaceManager
    from agentmem.types import SpaceCreate

    settings = get_settings().model_copy(update={"data_dir": tmp_path})
    manager = SpaceManager(settings)
    space = await manager.create_space(SpaceCreate(name="受害者", domain="药物发现"))
    payload = await manager.export_zip(space.id)

    source = zipfile.ZipFile(io.BytesIO(payload))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == "meta.db":
                path = tmp_path / "tampered.db"
                path.write_bytes(data)
                connection = sqlite3.connect(path)
                connection.execute("UPDATE spaces SET id = ?", (space_id,))
                connection.commit()
                connection.close()
                data = path.read_bytes()
            target.writestr(item, data)
    return buffer.getvalue()


@pytest.mark.parametrize("hostile_id", ["../../evil", "/tmp/absolute", "not-a-ulid"])
async def test_import_rejects_hostile_space_id(tmp_path: Path, hostile_id: str) -> None:
    """导入的 Space id 不可信：它会被拼进文件系统路径。

    这不是假想威胁——``spaces/<id>`` 直接拼路径，``../../evil`` 就能把整个
    Space 目录写到 data 之外；即使侥幸没越界，一个非 ULID 的 id 也会污染
    全局注册表，让后续所有按 id 拼出来的路径变得不可预期。
    """
    from agentmem.config import get_settings
    from agentmem.space.manager import SpaceManager

    payload = await _tampered_archive(tmp_path, hostile_id)
    settings = get_settings().model_copy(update={"data_dir": tmp_path})
    manager = SpaceManager(settings)

    with pytest.raises(ValidationError):
        await manager.import_zip(payload)


def test_space_dir_refuses_to_leave_the_root(tmp_path: Path) -> None:
    """``space_dir`` 自己也要挡：调用方未必都先校验过 id。"""
    from agentmem.config import get_settings
    from agentmem.space.manager import SpaceManager

    settings = get_settings().model_copy(update={"data_dir": tmp_path})
    manager = SpaceManager(settings)

    with pytest.raises(ValidationError):
        manager.space_dir("../../evil")


def test_import_rejects_oversized_archive(tmp_path: Path) -> None:
    """压缩包解包后的体量要设上限：压缩比能到上千倍，几百 KB 的包可以炸出几十 GB。"""
    import io
    import zipfile

    from agentmem.space import archive as archive_module
    from agentmem.space.archive import import_space_zip

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        # 高度可压缩的内容：磁盘上很小，解包后很大
        handle.writestr("raw/big.bin", b"\0" * (8 * 1024 * 1024))
        handle.writestr("meta.db", b"x")
    payload = buffer.getvalue()
    assert len(payload) < 100_000, "前提：压缩后很小"

    original = archive_module.MAX_TOTAL_BYTES
    archive_module.MAX_TOTAL_BYTES = 1024 * 1024
    try:
        with pytest.raises(ValidationError):
            import_space_zip(payload, tmp_path / "staging")
    finally:
        archive_module.MAX_TOTAL_BYTES = original


def test_import_ignores_oversized_entries_outside_the_space_layout(tmp_path: Path) -> None:
    """被跳过的条目不该拖累导入：夹带一个大文件不等于这个归档有问题。"""
    import io
    import zipfile

    from agentmem.space import archive as archive_module
    from agentmem.space.archive import import_space_zip

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as handle:
        handle.writestr("readme.txt", "无关文件".encode())
        handle.writestr("meta.db", b"x")
    target = tmp_path / "staging"

    original = archive_module.MAX_TOTAL_BYTES
    archive_module.MAX_TOTAL_BYTES = 1
    try:
        import_space_zip(buffer.getvalue(), target)
    finally:
        archive_module.MAX_TOTAL_BYTES = original

    assert (target / "meta.db").is_file()
    assert not (target / "readme.txt").exists()


def test_locate_raw_file_survives_relocation_and_refuses_outside_paths(tmp_path: Path) -> None:
    """原始文件路径失效时按文件名回 raw/ 找；指向 raw/ 外的路径一律不读。

    实测库里粘贴文档存相对路径、上传文件存绝对路径：换目录启动或把备份还原到别的
    机器后两种都失效，重新解析报「原始文件不存在」，可文件就在 raw/ 里。
    """
    from agentmem.security import locate_raw_file

    raw = tmp_path / "spaces" / "s1" / "raw"
    raw.mkdir(parents=True)
    (raw / "01DOC-paste.md").write_text("正文", encoding="utf-8")

    # 另一台机器上的绝对路径 / 另一个启动目录下的相对路径：按文件名找回
    assert (
        locate_raw_file(raw, "/Users/someone/old/data/spaces/s1/raw/01DOC-paste.md")
        == (raw / "01DOC-paste.md").resolve()
    )
    assert (
        locate_raw_file(raw, "data/spaces/s1/raw/01DOC-paste.md")
        == (raw / "01DOC-paste.md").resolve()
    )
    # 导入包里被构造的越界路径：不能照着读
    secret = tmp_path / "secret.txt"
    secret.write_text("不该被读到", encoding="utf-8")
    assert locate_raw_file(raw, str(secret)) is None
    assert locate_raw_file(raw, "../../../secret.txt") is None
    assert locate_raw_file(raw, None) is None
