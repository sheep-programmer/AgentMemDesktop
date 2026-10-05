"""§1 系统 / System。"""

from __future__ import annotations

from fastapi import APIRouter

from agentmem.types import (
    CapabilitiesResponse,
    HealthResponse,
    ProviderAlertsResponse,
    StatsResponse,
)

from ..deps import RuntimeDep

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse, summary="健康检查")
async def health(runtime: RuntimeDep) -> HealthResponse:
    """返回服务状态、版本与运行时长。"""
    from agentmem import __version__

    return HealthResponse(status="ok", version=__version__, uptime_ms=runtime.uptime_ms)


@router.get("/capabilities", response_model=CapabilitiesResponse, summary="后端能力声明")
async def capabilities(runtime: RuntimeDep) -> CapabilitiesResponse:
    """前端据此显隐功能（重排、本地向量、docling 解析、知识图谱）。"""
    return runtime.capabilities()


@router.get("/system/alerts", response_model=ProviderAlertsResponse, summary="Provider 失联告警")
async def provider_alerts(runtime: RuntimeDep) -> ProviderAlertsResponse:
    """最近半小时里失败过的 provider，以及它是否已经恢复。

    provider 挂掉时，用户此前看到的只有「回答失败」：既不知道是哪一个挂了，也不知道
    要不要去改配置。这里把用量记录里已有的失败如实汇总出来。
    """
    return await runtime.provider_alerts()


@router.get("/stats", response_model=StatsResponse, summary="全局统计")
async def stats(runtime: RuntimeDep) -> StatsResponse:
    """Space 数、文档数、chunk 数、磁盘占用。"""
    return await runtime.stats()
