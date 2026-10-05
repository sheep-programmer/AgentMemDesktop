"""FastAPI 应用入口：中间件、统一异常处理、结构化日志、静态资源托管。"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from agentmem.config import Settings, get_settings
from agentmem.errors import AgentMemError, InternalError
from agentmem.ingest.bus import IngestBus
from agentmem.security import redact_sensitive
from agentmem.space.runtime import Runtime

from .local_guard import install_local_guard
from .routers import build_router
from .static import FrontendFiles

logger = structlog.get_logger(__name__)

#: 未实现的路由返回的错误码（不在 `03-API-SPEC.md` §9 表内，见 CHANGELOG-INTERFACE）
NOT_IMPLEMENTED_CODE = "NOT_IMPLEMENTED"


def apply_security_headers(response: Response) -> None:
    """统一浏览器安全响应头；不启用 HSTS，因本地开发服务通常是 HTTP。"""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")


def configure_logging(settings: Settings) -> None:
    """配置 structlog 结构化日志。"""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(colors=False)
            if settings.is_dev
            else structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def error_response(error: AgentMemError) -> JSONResponse:
    """把 AgentMem 异常翻译成统一错误体。"""
    return JSONResponse(
        status_code=error.http_status,
        content=error.to_response().model_dump(mode="json"),
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """构造 FastAPI 应用。

    Args:
        settings: 全局设置；缺省从环境变量加载。
    """
    resolved = settings or get_settings()
    configure_logging(resolved)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime = Runtime(resolved)
        await runtime.start()
        bus = IngestBus(concurrency=resolved.ingest_concurrency)
        app.state.runtime = runtime
        app.state.bus = bus
        logger.info("api_started", port=resolved.port, env=resolved.env)
        try:
            yield
        finally:
            await bus.shutdown()
            await runtime.stop()
            logger.info("api_stopped")

    app = FastAPI(
        title="AgentMem API",
        version="0.1.0",
        description="本地优先的可进化 AI 知识库后端",
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    if resolved.is_dev:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=resolved.cors_origin_list,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # 没有鉴权的本地 API 要防跨站请求与 DNS 重绑定，见 local_guard 模块说明
    install_local_guard(app, resolved, error_response)

    @app.middleware("http")
    async def bind_log_context(request: Request, call_next: Any) -> Any:
        request_id = uuid4().hex
        request.state.request_id = request_id
        started = time.perf_counter()
        structlog.contextvars.bind_contextvars(
            path=request.url.path, method=request.method, request_id=request_id
        )
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            apply_security_headers(response)
            if request.url.path.startswith("/api/"):
                logger.info(
                    "request_response",
                    status=response.status_code,
                    response_start_ms=round((time.perf_counter() - started) * 1000, 2),
                )
            return response
        finally:
            structlog.contextvars.clear_contextvars()

    @app.exception_handler(AgentMemError)
    async def handle_agentmem_error(request: Request, exc: AgentMemError) -> JSONResponse:
        logger.warning("request_failed", code=exc.code, message=exc.message, path=request.url.path)
        return error_response(exc)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        from agentmem.errors import ValidationError as AgentMemValidationError

        # ⚠️ Pydantic v2 的 errors() 默认带 `input`，即用户刚提交的原始值。
        # 直接回显会让一次格式错误的 POST /providers 把明文 api_key 打进 422 响应体。
        error = AgentMemValidationError(
            "请求参数校验失败",
            detail={"errors": redact_sensitive(exc.errors())},
        )
        return error_response(error)

    @app.exception_handler(NotImplementedError)
    async def handle_not_implemented(request: Request, exc: NotImplementedError) -> JSONResponse:
        logger.info("route_not_implemented", path=request.url.path)
        return JSONResponse(
            status_code=501,
            content={
                "error": {
                    "code": NOT_IMPLEMENTED_CODE,
                    "message": str(exc) or "该功能尚未实现",
                    "detail": {"path": request.url.path},
                }
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", uuid4().hex)
        logger.exception("unhandled_error", path=request.url.path, request_id=request_id)
        response = error_response(InternalError(f"内部错误：{type(exc).__name__}"))
        response.headers["X-Request-ID"] = request_id
        apply_security_headers(response)
        return response

    app.include_router(build_router())
    _mount_frontend(app, resolved)
    return app


def _mount_frontend(app: FastAPI, settings: Settings) -> None:
    """生产模式下托管前端产物；目录不存在时静默跳过。"""
    if settings.is_dev:
        return
    dist = Path(settings.web_dist_dir)
    if not dist.is_dir():
        logger.info("web_dist_missing", path=str(dist))
        return
    app.mount("/", FrontendFiles(directory=dist, html=True), name="web")


app = create_app()
