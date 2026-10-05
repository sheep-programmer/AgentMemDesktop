"""§3 Space。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import nullcontext
from typing import Annotated

from fastapi import APIRouter, File, Query, UploadFile
from fastapi.responses import Response

from agentmem.errors import ValidationError
from agentmem.ingest.batch import run_documents
from agentmem.ingest.chunk import CHUNKER_VERSION
from agentmem.ingest.pipeline import IngestPipeline
from agentmem.retrieve.session import generation_registry
from agentmem.types import (
    DeleteResponse,
    ImportResponse,
    Persona,
    PersonaSuggestion,
    PersonaSuggestRequest,
    RetrievalSettings,
    SettingsUpdate,
    Space,
    SpaceCreate,
    SpaceDetail,
    SpaceSummary,
    SpaceUpdate,
    SpaceYaml,
)

from ..deps import BusDep, RuntimeDep
from ..sse import SseEvent, event, sse_response

router = APIRouter(prefix="/spaces", tags=["spaces"])


async def _summary(runtime: RuntimeDep, space: Space) -> SpaceSummary:
    """补上聚合计数。"""
    database = await runtime.spaces.space_db(space.id)
    snapshot = await database.expertise.latest(space.id)
    return SpaceSummary(
        **space.model_dump(),
        doc_count=await database.documents.count(),
        insight_count=await database.insights.count(),
        expertise_overall=snapshot.overall if snapshot else None,
    )


@router.get("", response_model=list[SpaceSummary], summary="Space 列表")
async def list_spaces(runtime: RuntimeDep) -> list[SpaceSummary]:
    """附带 doc_count、insight_count、expertise_overall。"""
    spaces = await runtime.spaces.list_spaces()
    return [await _summary(runtime, space) for space in spaces]


@router.post("", response_model=Space, status_code=201, summary="新建 Space")
async def create_space(runtime: RuntimeDep, payload: SpaceCreate) -> Space:
    """新建 Space 并生成初始 Persona。"""
    return await runtime.spaces.create_space(payload)


@router.post("/demo", response_model=Space, status_code=201, summary="铺设示例 Space")
async def create_demo_space(runtime: RuntimeDep) -> Space:
    """铺一个填满示例数据的 Space（幂等：同名已存在就返回它）。

    第一次打开应用时六个页面全是空态，用户得先配模型、投喂资料才看得到东西。
    这个入口让「示例」一键可达，不需要任何 provider。
    """
    from agentmem.space.demo import seed_demo_space

    return await seed_demo_space(runtime)


@router.post("/import", response_model=ImportResponse, summary="导入 Space（zip）")
async def import_space(runtime: RuntimeDep, file: Annotated[UploadFile, File()]) -> ImportResponse:
    """导入 zip 包；包含 raw + meta.db + vectors + space.yaml。"""
    space = await runtime.spaces.import_zip(await file.read())
    database = await runtime.spaces.space_db(space.id)
    return ImportResponse(
        space_id=space.id,
        name=space.name,
        document_count=await database.documents.count(),
    )


@router.get("/{space_id}", response_model=SpaceDetail, summary="Space 详情")
async def get_space(runtime: RuntimeDep, space_id: str) -> SpaceDetail:
    """含 space.yaml 解析后的 persona / retrieval / models。"""
    space = await runtime.spaces.require_space(space_id)
    summary = await _summary(runtime, space)
    config = await runtime.spaces.read_config(space_id)
    return SpaceDetail(**summary.model_dump(), config=config)


@router.patch("/{space_id}", response_model=Space, summary="修改 Space")
async def update_space(runtime: RuntimeDep, space_id: str, payload: SpaceUpdate) -> Space:
    """修改基本信息。"""
    await runtime.spaces.require_space(space_id)
    return await runtime.spaces.update_space(space_id, payload)


@router.delete("/{space_id}", response_model=DeleteResponse, summary="删除 Space")
async def delete_space(
    runtime: RuntimeDep,
    bus: BusDep,
    space_id: str,
    purge: Annotated[bool, Query(description="是否同时删除磁盘数据，默认删")] = True,
) -> DeleteResponse:
    """删除 Space：默认连同磁盘数据一起删（``purge=false`` 只摘注册表）。

    重建索引期间拒绝；还在跑的摄取 / 抽取任务先取消并等它们退出，再关库删目录。
    """
    await runtime.spaces.require_space(space_id)
    runtime.require_unlocked(space_id)
    await bus.cancel_space(space_id)
    await generation_registry().cancel_and_wait(space_id=space_id)
    # Runtime provides a write gate in production. Keeping the small fallback makes this
    # route composable with maintenance/test runtimes that only expose SpaceManager.
    maintain = getattr(runtime, "maintain_space", None)
    gate = maintain(space_id) if callable(maintain) else nullcontext()
    with gate:
        deleted = await runtime.spaces.delete_space(space_id, purge=purge)
        return DeleteResponse(deleted=deleted, id=space_id)


@router.get("/{space_id}/persona", response_model=Persona, summary="获取 L4 画像")
async def get_persona(runtime: RuntimeDep, space_id: str) -> Persona:
    """取该 Space 的 Persona。"""
    await runtime.spaces.require_space(space_id)
    config = await runtime.spaces.read_config(space_id)
    return config.persona


@router.put("/{space_id}/persona", response_model=Persona, summary="更新 L4 画像")
async def update_persona(runtime: RuntimeDep, space_id: str, payload: Persona) -> Persona:
    """写回 ``space.yaml`` 的 persona 段。"""
    await runtime.spaces.require_space(space_id)
    config = await runtime.spaces.update_persona(space_id, payload)
    return config.persona


@router.post(
    "/{space_id}/persona/suggest", response_model=PersonaSuggestion, summary="起草 Persona"
)
async def suggest_persona(
    runtime: RuntimeDep, space_id: str, payload: PersonaSuggestRequest
) -> PersonaSuggestion:
    """根据已有文档起草 Persona（返回草稿，不落库）。

    本期返回基于 Space 名称与领域的模板草稿；模型驱动的版本由主控的
    ``core/expert`` 提供，届时替换本函数体即可。
    """
    space = await runtime.spaces.require_space(space_id)
    persona = runtime.spaces.default_persona(space)
    if payload.instructions:
        persona = persona.model_copy(
            update={"role_description": f"{persona.role_description}\n\n{payload.instructions}"}
        )
    return PersonaSuggestion(persona=persona, rationale="基于 Space 名称与领域生成的初始草稿")


@router.get("/{space_id}/settings", response_model=RetrievalSettings, summary="检索参数")
async def get_retrieval_settings(runtime: RuntimeDep, space_id: str) -> RetrievalSettings:
    """取 space.yaml 中的检索参数。"""
    await runtime.spaces.require_space(space_id)
    config = await runtime.spaces.read_config(space_id)
    return config.retrieval


@router.put("/{space_id}/settings", response_model=SpaceYaml, summary="更新检索参数")
async def update_retrieval_settings(
    runtime: RuntimeDep, space_id: str, payload: SettingsUpdate
) -> SpaceYaml:
    """更新检索参数与（可选的）模型角色覆盖。"""
    await runtime.spaces.require_space(space_id)
    config = await runtime.spaces.read_config(space_id)
    if payload.retrieval is not None:
        config.retrieval = payload.retrieval
    if payload.models is not None:
        config.models = payload.models
    runtime.invalidate_registries()
    return await runtime.spaces.write_config(space_id, config)


@router.post("/{space_id}/reindex", summary="重建切片与向量索引（SSE）")
async def reindex(
    runtime: RuntimeDep,
    space_id: str,
    only_stale: Annotated[
        bool, Query(description="只重做切片算法版本过期的文档；默认全部重建")
    ] = False,
) -> Response:
    """重建整个 Space 的切片与向量索引，通过 SSE 推送逐篇进度。

    **重切而不只是重嵌入**：切片规则变了以后，只有重切才能让 ``char_start`` /
    ``char_end`` 重新与原文对得上（引用高亮依赖它），旧切片也才可能被正确地切小、
    给表格补上表头。走的是解析缓存，不会重新调用解析器；每篇失败只记一条 error
    事件，不阻断其它文档。

    ``only_stale=true`` 只重做切片算法版本过期的文档（``meta.chunker_version`` 与当前
    版本不一致）。此时**不会**清空整张向量表——跳过的文档得保住它已有的向量，
    否则它们的检索能力会被这次「部分重建」抹掉。失败 / 中断文档也会被选中，
    即使切片版本已更新，仍会补做未完成的向量化。
    """
    space = await runtime.spaces.require_space(space_id)
    database = await runtime.spaces.space_db(space_id)
    registry = await runtime.registry_for_space(space_id)

    async def generate() -> AsyncIterator[SseEvent]:
        # 重建会先 drop 掉整张向量表再逐篇重构：这期间放新的摄取进来，写进去的向量
        # 要么被 drop 抹掉、要么与重构互相覆盖，最后得到一个自相矛盾的索引。
        # 客户端断开时 sse_response 会关闭这个生成器，finally 保证锁一定被释放。
        with runtime.maintain_space(space_id) as token:
            pipeline = IngestPipeline(database, registry, runtime.settings, operation_token=token)
            # 翻页取全部：此前只取前 1000 篇，全量重建却先 drop 了整张向量表，
            # 超出的文档切片还在、向量没了，从此在向量检索里静默消失
            documents = []
            cursor: str | None = None
            while True:
                page, _total, cursor = await database.documents.list_by_space(
                    space.id, limit=200, cursor=cursor
                )
                documents.extend(page)
                if not cursor:
                    break
            total = len(documents)
            if only_stale:
                documents = [
                    item
                    for item in documents
                    if item.status != "ready" or (item.meta.chunker_version or 0) != CHUNKER_VERSION
                ]
                total = len(documents)
            else:
                # 全量重建才清空向量表；部分重建时必须保住未重做文档的向量
                vectors = database.vectors
                if vectors is not None:
                    await vectors.drop("chunks_vec")
            yield event("begin", {"total": total, "only_stale": only_stale})
            done = 0
            # 按篇并发：一篇的耗时几乎全在等模型，等待可以重叠。上限沿用
            # ingest_concurrency，再高就轮到向量表与模型服务被压垮
            async for outcome in run_documents(
                pipeline,
                [document.id for document in documents],
                stages=["chunking", "embedding"],
                concurrency=runtime.settings.ingest_concurrency,
            ):
                if not outcome.ok:
                    yield event(
                        "error", {"document_id": outcome.document_id, "message": outcome.error}
                    )
                    continue
                done += 1
                yield event(
                    "progress",
                    {
                        "document_id": outcome.document_id,
                        "stage": "embedding",
                        "done": done,
                        "total": total,
                        "percent": round(done / total * 100, 2) if total else 100.0,
                    },
                )
            yield event("done", {"total": total, "reindexed": done})

    return sse_response(generate())


@router.get("/{space_id}/export", summary="导出 Space（zip）")
async def export_space(runtime: RuntimeDep, bus: BusDep, space_id: str) -> Response:
    """导出 raw + meta.db + vectors + space.yaml。

    摄取未完成时拒绝导出：向量是在摄取末尾才写进 LanceDB 的，此刻打包会得到一个
    **缺向量的残包**——而残包还原回来时不会有任何报错，检索只是静默退化成纯全文，
    混合检索这条腿就这么没了。实测复现过：文档刚粘贴完就导出，包里连
    ``vectors/`` 目录都没有，还原后 ``legs`` 只剩 ``fts:1``。
    """
    space = await runtime.spaces.require_space(space_id)
    # 重建索引刚把向量表删掉、第一篇还没翻到 chunking 时，下面的状态检查查不到任何
    # 进行中的文档；导出还会关掉重建正在用的连接，让重建中途失败
    runtime.require_unlocked(space_id)
    if bus.running_tasks(space_id) > 0:
        raise ValidationError(
            "这个知识空间还有后台任务在跑（导入、抽取卡片等），请等它们完成后再导出",
            detail={"space_id": space_id, "running": bus.running_tasks(space_id)},
        )
    database = await runtime.spaces.space_db(space_id)
    busy = [
        status
        for status in ("pending", "parsing", "chunking", "embedding", "extracting")
        if (await database.documents.list_by_space(space_id, status=status, limit=1))[1] > 0
    ]
    if busy:
        raise ValidationError(
            "还有文档正在摄取，此时导出会得到缺少向量索引的残包；请等摄取完成后再导出",
            detail={"space_id": space_id, "busy_statuses": busy},
        )
    with runtime.maintain_space(space_id):
        payload = await runtime.spaces.export_zip(space_id)
        return Response(
            content=payload,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{space.id}.zip"'},
        )
