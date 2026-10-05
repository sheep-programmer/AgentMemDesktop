"""桌面生产服务支持直接打开和刷新前端路由，并能定位请求错误。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from agentmem.config import Settings
from apps.api.main import create_app


@pytest.fixture
def production_client(tmp_path: Path) -> Iterator[TestClient]:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>AgentMem workspace</html>", encoding="utf-8")
    (dist / "assets").mkdir()
    (dist / "assets" / "app.js").write_text("console.log('ready')", encoding="utf-8")
    models = tmp_path / "models.yaml"
    models.write_text("providers: []\nroles: {}", encoding="utf-8")
    settings = Settings(
        env="prod",
        data_dir=tmp_path / "data",
        models_config=models,
        web_dist_dir=dist,
        warmup_on_start=False,
    )
    app = create_app(settings)

    @app.get("/api/v1/test-error")
    async def test_error() -> None:
        raise RuntimeError("测试内部错误")

    # 测试专用路由必须放在根目录静态挂载之前。
    app.router.routes.insert(0, app.router.routes.pop())

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.mark.parametrize("route", ["/", "/settings", "/s/example/library", "/s/example/chat"])
def test_frontend_routes_can_be_refreshed(production_client: TestClient, route: str) -> None:
    response = production_client.get(route, headers={"Accept": "text/html"})
    assert response.status_code == 200
    assert "AgentMem workspace" in response.text
    assert response.headers["content-type"].startswith("text/html")


@pytest.mark.parametrize("route", ["/api/v1/missing", "/assets/missing.js", "/pdfjs/missing"])
def test_missing_resources_keep_404(production_client: TestClient, route: str) -> None:
    response = production_client.get(route, headers={"Accept": "text/html"})
    assert response.status_code == 404
    assert "AgentMem workspace" not in response.text


def test_request_ids_cover_success_and_failure(production_client: TestClient) -> None:
    ids = []
    for route, status in [("/api/v1/health", 200), ("/api/v1/test-error", 500)]:
        response = production_client.get(route)
        assert response.status_code == status
        request_id = response.headers["x-request-id"]
        assert UUID(hex=request_id).version == 4
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        ids.append(request_id)
    assert ids[0] != ids[1]


def test_static_files_and_head_requests(production_client: TestClient) -> None:
    assert production_client.get("/assets/app.js").text == "console.log('ready')"
    head = production_client.head("/settings", headers={"Accept": "text/html"})
    assert head.status_code == 200
    assert head.content == b""
    assert (
        production_client.get("/settings", headers={"Accept": "application/json"}).status_code
        == 404
    )
