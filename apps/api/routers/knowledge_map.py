"""知识图谱：领域 → 主题 / 文档 → 卡片 → 经验的网络、进化脉络与掌握程度。

实现落在 ``core/expert/knowledge_map``；本模块只做「解包请求 → 调 core」。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from agentmem.expert.knowledge_map import (
    DEFAULT_MAX_CARDS,
    KnowledgeMapResponse,
    build_knowledge_map,
)

from ..deps import RuntimeDep

router = APIRouter(prefix="/spaces/{space_id}", tags=["knowledge-map"])


@router.get("/knowledge-map", response_model=KnowledgeMapResponse, summary="知识图谱")
async def knowledge_map(
    space_id: str,
    runtime: RuntimeDep,
    max_cards: Annotated[
        int, Query(description="最多画多少张卡片（按置信度取前 N），超出 1~1000 时自动夹紧")
    ] = DEFAULT_MAX_CARDS,
) -> KnowledgeMapResponse:
    """知识网络 + 进化脉络 + 各主题掌握度，一次取齐。

    只读，不调用生成模型；卡片归属主题时用本地 embedding（向量有缓存）。
    ``max_cards`` 越界时夹紧而不是报 422：这是个「画多少」的展示参数，
    给大了就画到上限，没有理由让整页加载失败。
    """
    space = await runtime.spaces.require_space(space_id)
    database = await runtime.space_db(space_id)
    embed, embed_key = await runtime.topic_embedder(space_id)
    return await build_knowledge_map(
        database, space, max_cards=max_cards, embed=embed, embed_key=embed_key
    )
