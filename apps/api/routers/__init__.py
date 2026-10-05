"""路由汇总。"""

from fastapi import APIRouter

from . import chat, documents, evolve, expert, knowledge_map, memory, providers, spaces, system

API_PREFIX = "/api/v1"


def build_router() -> APIRouter:
    """按 ``03-API-SPEC.md`` §1~§8 组装路由。"""
    router = APIRouter(prefix=API_PREFIX)
    router.include_router(system.router)
    router.include_router(providers.router)
    router.include_router(spaces.router)
    router.include_router(documents.router)
    router.include_router(documents.ingest_router)
    router.include_router(chat.router)
    router.include_router(memory.router)
    router.include_router(evolve.router)
    router.include_router(expert.router)
    router.include_router(knowledge_map.router)
    return router


__all__ = ["API_PREFIX", "build_router"]
