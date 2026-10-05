"""依赖注入：提供 core 层单例（运行时、数据库、注册表、摄取流水线）。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from agentmem.config import Settings
from agentmem.ingest.bus import IngestBus
from agentmem.ingest.pipeline import IngestPipeline
from agentmem.providers.registry import ProviderRegistry
from agentmem.space.runtime import Runtime
from agentmem.store import Database


def get_runtime(request: Request) -> Runtime:
    """取进程内共享的 Runtime。"""
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:  # pragma: no cover - 只在未走 lifespan 时发生
        raise RuntimeError("Runtime 尚未初始化")
    return runtime  # type: ignore[no-any-return]


def get_settings_dep(request: Request) -> Settings:
    """取全局设置。"""
    return get_runtime(request).settings


def get_bus(request: Request) -> IngestBus:
    """取摄取进度总线。"""
    bus = getattr(request.app.state, "bus", None)
    if bus is None:  # pragma: no cover
        raise RuntimeError("IngestBus 尚未初始化")
    return bus  # type: ignore[no-any-return]


RuntimeDep = Annotated[Runtime, Depends(get_runtime)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
BusDep = Annotated[IngestBus, Depends(get_bus)]


async def get_space_database(space_id: str, runtime: RuntimeDep) -> Database:
    """按路径参数取 Space 数据库，Space 不存在时抛 404。"""
    await runtime.spaces.require_space(space_id)
    return await runtime.spaces.space_db(space_id)


async def get_space_registry(space_id: str, runtime: RuntimeDep) -> ProviderRegistry:
    """取某 Space 的 Provider 注册表（含 space.yaml 的模型覆盖）。"""
    await runtime.spaces.require_space(space_id)
    return await runtime.registry_for_space(space_id)


async def get_pipeline(space_id: str, runtime: RuntimeDep) -> IngestPipeline:
    """取某 Space 的摄取流水线。"""
    await runtime.spaces.require_space(space_id)
    database = await runtime.spaces.space_db(space_id)
    registry = await runtime.registry_for_space(space_id)
    return IngestPipeline(database, registry, runtime.settings)


DatabaseDep = Annotated[Database, Depends(get_space_database)]
RegistryDep = Annotated[ProviderRegistry, Depends(get_space_registry)]
PipelineDep = Annotated[IngestPipeline, Depends(get_pipeline)]
