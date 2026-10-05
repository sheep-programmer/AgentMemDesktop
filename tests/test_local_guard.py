"""本地 API 来源守卫：跨站请求与 DNS 重绑定要被挡住，正常调用不受影响。"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from agentmem.config import Settings
from apps.api.main import create_app

API = "/api/v1"


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _space(client: TestClient) -> str:
    return str(client.post(f"{API}/spaces", json={"name": "守卫", "domain": "测试"}).json()["id"])


def test_dns_rebinding_host_is_rejected(client: TestClient) -> None:
    """攻击者域名解析到 127.0.0.1 时浏览器视为同源：Host 不是本机就得拒，读写都拒。

    修复前实测：``Host: rebind.evil.example`` 下 DELETE 空间返回 200。
    """
    space_id = _space(client)
    rebind = {"Host": "rebind.evil.example"}
    deleted = client.delete(f"{API}/spaces/{space_id}", headers=rebind)
    assert deleted.status_code == 403
    assert deleted.json()["error"]["code"] == "FORBIDDEN_ORIGIN"
    assert client.get(f"{API}/spaces", headers=rebind).status_code == 403, "读也不行"
    assert client.get(f"{API}/spaces/{space_id}").status_code == 200, "空间还在"


def test_cross_site_write_is_rejected_but_reads_and_scripts_are_not(client: TestClient) -> None:
    """跨站网页发来的写请求拒绝；本机前端、命令行（不带 Origin）照常。

    修复前实测：不带请求体的跨站 POST /reindex 返回 200，副作用照样发生。
    """
    space_id = _space(client)
    evil = {"Origin": "https://evil.example"}
    assert client.post(f"{API}/spaces/{space_id}/reindex", headers=evil).status_code == 403
    assert client.delete(f"{API}/spaces/{space_id}", headers={"Origin": "null"}).status_code == 403
    # 读请求跨站读不到响应（CORS 挡），不必在这里拒
    assert client.get(f"{API}/spaces", headers=evil).status_code == 200

    # 本机前端（开发服务器与同源部署）与不带 Origin 的脚本都放行
    for origin in ("http://localhost:5173", "http://127.0.0.1:8765", "http://[::1]:5173"):
        response = client.patch(
            f"{API}/spaces/{space_id}", json={"name": "改名"}, headers={"Origin": origin}
        )
        assert response.status_code == 200, origin
    assert client.patch(f"{API}/spaces/{space_id}", json={"name": "脚本改名"}).status_code == 200


def test_wildcard_bind_skips_host_check(settings: Settings) -> None:
    """监听 0.0.0.0 是有意开给局域网：主机名无从预知，只保留 Origin 检查。"""
    lan = settings.model_copy(update={"host": "0.0.0.0"})
    with TestClient(create_app(lan)) as client:
        assert client.get(f"{API}/spaces", headers={"Host": "192.168.1.20:8765"}).status_code == 200
        blocked = client.post(
            f"{API}/spaces",
            json={"name": "x", "domain": "y"},
            headers={"Origin": "https://evil.example"},
        )
        assert blocked.status_code == 403
