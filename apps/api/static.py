"""生产环境的单页应用路由回退，保留 API 与静态资源的真实 404。"""

from pathlib import Path

from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope


class FrontendFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            accepts_html = "text/html" in Headers(scope=scope).get("accept", "")
            is_api = path == "api" or path.startswith("api/")
            is_asset = path.startswith(("assets/", "pdfjs/")) or bool(Path(path).suffix)
            if (
                exc.status_code == 404
                and scope["method"] in {"GET", "HEAD"}
                and accepts_html
                and not is_api
                and not is_asset
            ):
                return await super().get_response("index.html", scope)
            raise
