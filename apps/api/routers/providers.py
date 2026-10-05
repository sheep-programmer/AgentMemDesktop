"""§2 模型 Provider。"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Path, Query

from agentmem.config import ModelsConfig
from agentmem.errors import NotFoundError, ProviderInUseError, ValidationError
from agentmem.providers.discover import discover_models
from agentmem.providers.health import check_config
from agentmem.providers.local_agents import resolve_api_key, scan_local_agents
from agentmem.types import (
    DeleteResponse,
    DiscoverRequest,
    DiscoverResponse,
    LocalAgentImportRequest,
    LocalAgentScanResponse,
    ProviderConfig,
    ProviderCreate,
    ProviderHealth,
    ProviderPublic,
    ProviderUpdate,
    RoleBindings,
    RolesUpdateRequest,
    RolesUpdateResponse,
    UsageResponse,
)

from ..deps import RuntimeDep

router = APIRouter(prefix="/providers", tags=["providers"])


def _find(config: ModelsConfig, provider_id: str) -> ProviderConfig:
    for item in config.providers:
        if item.id == provider_id:
            return item
    raise NotFoundError("Provider", provider_id)


@router.get("", response_model=list[ProviderPublic], summary="列出全部 provider")
async def list_providers(runtime: RuntimeDep) -> list[ProviderPublic]:
    """返回 provider 的 kind / adapter / model / enabled。密钥只返回掩码。"""
    return [item.to_public() for item in runtime.models_config.providers]


@router.post("", response_model=ProviderPublic, status_code=201, summary="新增 provider")
async def create_provider(runtime: RuntimeDep, payload: ProviderCreate) -> ProviderPublic:
    """新增 provider 并写回 ``config/models.yaml``。"""
    config = runtime.models_config
    if any(item.id == payload.id for item in config.providers):
        raise ValidationError("provider id 已存在", detail={"provider_id": payload.id})
    created = ProviderConfig(**payload.model_dump())
    updated = ModelsConfig(
        providers=[*config.providers, created],
        roles=config.roles,
        fallbacks=config.fallbacks,
    )
    runtime.save_models(updated)
    return created.to_public()


@router.patch("/{provider_id}", response_model=ProviderPublic, summary="修改 provider")
async def update_provider(
    runtime: RuntimeDep,
    provider_id: Annotated[str, Path(description="provider id")],
    payload: ProviderUpdate,
) -> ProviderPublic:
    """按字段部分更新 provider 配置。"""
    config = runtime.models_config
    existing = _find(config, provider_id)
    fields = payload.model_dump(exclude_unset=True)
    merged = existing.model_dump()
    merged.update({key: value for key, value in fields.items() if value is not None})
    # 用户换了新密钥时必须丢掉旧的 ${VAR} 来源记录，
    # 否则写回时会拿旧占位符覆盖新密钥，改动静默丢失。
    if fields.get("api_key") is not None and fields["api_key"] != existing.api_key:
        merged["api_key_ref"] = None
    updated_provider = ProviderConfig.model_validate(merged)
    providers = [updated_provider if item.id == provider_id else item for item in config.providers]
    runtime.save_models(
        ModelsConfig(providers=providers, roles=config.roles, fallbacks=config.fallbacks)
    )
    return updated_provider.to_public()


@router.delete("/{provider_id}", response_model=DeleteResponse, summary="删除 provider")
async def delete_provider(
    runtime: RuntimeDep,
    provider_id: Annotated[str, Path(description="provider id")],
) -> DeleteResponse:
    """删除 provider；仍被角色引用时返回 ``PROVIDER_IN_USE``。"""
    config = runtime.models_config
    existing = _find(config, provider_id)
    roles = runtime.registry().roles_in_use(provider_id)
    if roles:
        raise ProviderInUseError(provider_id, roles)
    providers = [item for item in config.providers if item.id != existing.id]
    runtime.save_models(
        ModelsConfig(providers=providers, roles=config.roles, fallbacks=config.fallbacks)
    )
    return DeleteResponse(deleted=True, id=provider_id)


@router.post("/{provider_id}/health", response_model=ProviderHealth, summary="实测连通性")
async def provider_health(
    runtime: RuntimeDep,
    provider_id: Annotated[str, Path(description="provider id")],
) -> ProviderHealth:
    """返回延迟、可用性、实际返回的模型名。"""
    config = _find(runtime.models_config, provider_id)
    return await check_config(config)


@router.post("/discover", response_model=DiscoverResponse, summary="探测可用模型列表")
async def discover(runtime: RuntimeDep, payload: DiscoverRequest) -> DiscoverResponse:
    """给定 adapter 与 base_url，探测该端点的模型列表（如「一键发现本机 Ollama 模型」）。"""
    return await discover_models(payload.adapter, payload.base_url, payload.api_key)


@router.get(
    "/local-agents",
    response_model=LocalAgentScanResponse,
    summary="扫描本机已有的 Agent 配置",
)
async def scan_local_agent_configs(runtime: RuntimeDep) -> LocalAgentScanResponse:
    """读取本机 Claude Code / Codex / Continue 的配置与相关环境变量，列出可复用的 provider。

    **只读白名单内的几个配置文件，不遍历主目录**；响应里返回 ``scanned_paths``
    让用户看到到底读了哪些。密钥不以明文返回：来自环境变量的只给变量名，
    配置里是字面量的只给掩码（见 ``LocalAgentCandidate`` 的说明）。
    """
    existing = {item.id for item in runtime.models_config.providers}
    return await asyncio.to_thread(scan_local_agents, existing)


@router.post(
    "/import-local",
    response_model=list[ProviderPublic],
    status_code=201,
    summary="导入本机 Agent 配置",
)
async def import_local_agents(
    runtime: RuntimeDep, payload: LocalAgentImportRequest
) -> list[ProviderPublic]:
    """把选中的本机候选导入为 provider。

    密钥优先写成 ``${VAR}`` 占位符（明文不落盘）；候选没有可用密钥来源时照样导入，
    由用户后续在界面上补填。已存在同 id 的候选会被跳过，不覆盖用户已有配置。
    """
    config = runtime.models_config
    existing = {item.id for item in config.providers}
    scan = await asyncio.to_thread(scan_local_agents, existing)
    wanted = set(payload.suggested_ids)
    selected = [c for c in scan.candidates if c.suggested_id in wanted]
    if not selected:
        raise ValidationError("没有匹配的候选可导入", detail={"requested": payload.suggested_ids})

    created: list[ProviderConfig] = []
    for candidate in selected:
        if candidate.suggested_id in existing:
            continue  # 已存在则跳过，绝不静默覆盖用户已有配置
        created.append(
            ProviderConfig(
                id=candidate.suggested_id,
                kind=candidate.kind,
                adapter=candidate.adapter,
                model=candidate.model,
                base_url=candidate.base_url,
                api_key=resolve_api_key(candidate),
                enabled=True,
            )
        )
    if not created:
        raise ValidationError("选中的候选都已存在", detail={"requested": payload.suggested_ids})

    runtime.save_models(
        ModelsConfig(
            providers=[*config.providers, *created],
            roles=config.roles,
            fallbacks=config.fallbacks,
        )
    )
    return [item.to_public() for item in created]


@router.get("/roles", response_model=RoleBindings, summary="当前角色绑定")
async def get_roles(runtime: RuntimeDep) -> RoleBindings:
    """返回 chat / fast / distill / judge / embedding / rerank 的绑定关系。"""
    return RoleBindings.model_validate(runtime.role_bindings())


@router.put("/roles", response_model=RolesUpdateResponse, summary="更新角色绑定")
async def update_roles(runtime: RuntimeDep, payload: RolesUpdateRequest) -> RolesUpdateResponse:
    """更新角色绑定；embedding 维度变化时提示需要重建索引。"""
    config = runtime.models_config
    previous_dimension = _embedding_dimension(runtime, config.roles.embedding)
    for provider_id in payload.roles.model_dump(exclude_none=True).values():
        _find(config, provider_id)
    new_dimension = _embedding_dimension(runtime, payload.roles.embedding)
    updated = ModelsConfig(
        providers=config.providers,
        roles=payload.roles,
        fallbacks=payload.fallbacks or config.fallbacks,
    )
    runtime.save_models(updated)
    requires_reindex = (
        previous_dimension is not None
        and new_dimension is not None
        and previous_dimension != new_dimension
    )
    affected: list[str] = []
    if requires_reindex:
        spaces = await runtime.spaces.list_spaces()
        affected = [space.id for space in spaces]
    return RolesUpdateResponse(
        roles=payload.roles, requires_reindex=requires_reindex, affected_spaces=affected
    )


def _embedding_dimension(runtime: RuntimeDep, provider_id: str | None) -> int | None:
    """取角色的向量维度；未显式配置且未加载过模型时为 ``None``。"""
    if not provider_id:
        return None
    try:
        return runtime.registry().embedding_dimension(provider_id)
    except Exception:  # 维度探测失败不应阻断角色切换
        return None


@router.get("/usage", response_model=UsageResponse, summary="用量统计")
async def usage(
    runtime: RuntimeDep,
    group_by: Annotated[str, Query(pattern="^(provider|model|purpose|kind)$")] = "provider",
    since: Annotated[int | None, Query(description="起始时间（Unix 毫秒）")] = None,
    kind: Annotated[
        str | None,
        Query(
            pattern="^(llm|embedding|rerank)$",
            description="限定能力类型；看缓存命中率时应传 llm",
        ),
    ] = None,
) -> UsageResponse:
    """按 provider / model / purpose 聚合 token 与耗时。

    ⚠️ 统计前缀缓存命中率时**必须**带 ``kind=llm``：embedding 与 rerank 没有
    前缀缓存，把它们算进分母只会得到一个被稀释的、没有意义的命中率。
    """
    database = await runtime.global_db()
    items = await database.usage.aggregate(group_by=group_by, since=since, kind=kind)
    return UsageResponse(
        group_by=group_by,
        items=items,
        total_tokens=sum(item.total_tokens for item in items),
    )


# ⚠️ 这条**必须**声明在 /roles、/usage、/discover 之后。
# FastAPI 按声明顺序匹配，路径参数路由若排在前面会把这些字面路径一并吃掉。
@router.get("/{provider_id}", response_model=ProviderPublic, summary="单个 provider 详情")
async def get_provider(
    runtime: RuntimeDep,
    provider_id: Annotated[str, Path(description="provider id")],
) -> ProviderPublic:
    """按 id 取单个 provider；不存在时返回 ``NOT_FOUND``。"""
    return _find(runtime.models_config, provider_id).to_public()
