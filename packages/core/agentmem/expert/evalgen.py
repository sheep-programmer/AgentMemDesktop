"""测验集与领域大纲生成 —— 专家度量化的基础设施。

- `generate_from_documents` 从资料出题（覆盖面广）。
- `generate_from_corrections` 从用户纠错出题（针对性最强，直接检验弱点是否修好）。
- `generate_outline` 生成领域知识大纲（coverage 维度与知识盲区的基准）。

全部复用 `agentmem.prompts` 的对应 builder，本模块只做数据组装与落库。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import structlog

from agentmem.evolve._persona import to_persona_spec, to_provider_messages
from agentmem.prompts import ChunkRef as PromptChunkRef
from agentmem.prompts import (
    CorrectionSample,
    build_domain_outline_messages,
    build_from_correction_messages,
    build_from_document_messages,
    extract_json,
)
from agentmem.providers.base import Message as ProviderMessage
from agentmem.providers.registry import ProviderRegistry
from agentmem.store import Database
from agentmem.types import EvalItem, EvalItemCreate, EvalSource, OutlineNode, Persona

logger = structlog.get_logger(__name__)

EVALGEN_TEMPERATURE = 0.4  # 出题可以稍有多样性
OUTLINE_TEMPERATURE = 0.3

#: 每批喂给出题 Prompt 的切片数
CHUNKS_PER_BATCH = 6


# OutlineNode 定义在公共类型层：仓储与 API 都要用它，留在这里会造成两份定义


#: 未指定文档时，最多从这么多篇最近入库的文档出题。
#: 不设上限的话，一个大 Space 会因为「每篇分到 1 道题」而跑很久，
#: 题量却没有变多。
DEFAULT_DOCUMENT_LIMIT = 10


async def generate_from_documents(
    *,
    database: Database,
    registry: ProviderRegistry,
    persona: Persona,
    space_id: str,
    document_ids: list[str],
    count: int,
) -> list[EvalItem]:
    """从文档的切片出题。

    ``document_ids`` 为空时退化为「最近入库的若干篇 ready 文档」：出题入口不带
    文档选择器时，静默产出 0 道题比多出几道更糟——用户点一下按钮什么也没发生，
    还不知道为什么。
    """
    document_ids = document_ids or await _recent_documents(database, space_id)
    route = registry.llm("distill", purpose="evalgen")
    spec = to_persona_spec(persona)
    produced: list[EvalItem] = []

    for document_id in document_ids:
        document = await database.documents.get(document_id)
        if document is None:
            continue
        chunks = await database.chunks.list_by_document(document_id)
        if not chunks:
            continue
        # 只喂前若干切片，够出题即可，不必把整篇灌进去
        batch = chunks[:CHUNKS_PER_BATCH]
        refs = [
            PromptChunkRef(
                marker=f"c{i}",
                chunk_id=c.id,
                document_title=document.title,
                heading_path=c.heading_path,
                page=c.page,
                content=c.content,
            )
            for i, c in enumerate(batch, start=1)
        ]
        messages = build_from_document_messages(
            persona=spec,
            document_title=document.title,
            chunks=refs,
            n=max(1, count // max(1, len(document_ids))),
        )
        completion = await route.chat(
            to_provider_messages(messages), temperature=EVALGEN_TEMPERATURE
        )
        produced.extend(
            await _persist_items(database, space_id, completion.content, "auto_from_doc")
        )

    logger.info(
        "evalgen_from_docs", space_id=space_id, docs=len(document_ids), produced=len(produced)
    )
    return produced


async def generate_from_corrections(
    *,
    database: Database,
    registry: ProviderRegistry,
    persona: Persona,
    space_id: str,
    limit: int = 20,
) -> list[EvalItem]:
    """从用户纠错出题：换个问法考同一个知识点，检验是否真的改正。"""
    samples = await _correction_samples(database, space_id, limit)
    if not samples:
        return []
    messages = build_from_correction_messages(persona=to_persona_spec(persona), samples=samples)
    route = registry.llm("distill", purpose="evalgen")
    completion = await route.chat(to_provider_messages(messages), temperature=EVALGEN_TEMPERATURE)
    items = await _persist_items(database, space_id, completion.content, "from_correction")
    logger.info("evalgen_from_corrections", space_id=space_id, produced=len(items))
    return items


@dataclass(frozen=True)
class OutlineResult:
    """一次大纲生成的结果。

    ``nodes`` 为空即失败，``failure_reason`` 写明模型到底说了什么——
    只报「没有返回可用的节点」的话，用户不知道该改领域名还是该重试。
    """

    nodes: list[OutlineNode]
    interpretation: str | None = None
    failure_reason: str | None = None


OUTLINE_RETRY_NUDGE = (
    "你上一条回复没有按要求输出 JSON。这一步不需要任何资料，凭你对该领域的常识即可；"
    "领域名不明确时按最合理的理解来列，并写进 domain_interpretation。"
    "现在只输出那个 JSON 对象，不要任何其他文字。"
)


async def generate_outline(
    *,
    registry: ProviderRegistry,
    persona: Persona,
    document_hints: list[str] | None = None,
) -> OutlineResult:
    """生成领域知识大纲（coverage 分母与知识盲区基准）。

    故意用模型的先验知识而非知识库内容——我们要一把外部尺子量出「缺了什么」。
    ``document_hints`` 只帮模型辨认领域。模型拒答或输出不成 JSON 时追问一次。
    """
    messages = to_provider_messages(
        build_domain_outline_messages(
            persona=to_persona_spec(persona), document_hints=document_hints
        )
    )
    route = registry.llm("distill", purpose="outline")
    reply = ""
    for attempt in range(2):
        completion = await route.chat(messages, temperature=OUTLINE_TEMPERATURE)
        reply = completion.content
        try:
            data = extract_json(reply)
        except ValueError as exc:
            logger.warning("outline_parse_failed", attempt=attempt, error=str(exc))
            messages = [
                *messages,
                ProviderMessage(role="assistant", content=reply),
                ProviderMessage(role="user", content=OUTLINE_RETRY_NUDGE),
            ]
            continue
        nodes = _outline_nodes(data)
        interpretation = str(data.get("domain_interpretation") or "").strip() or None
        if nodes:
            logger.info("outline_generated", nodes=len(nodes), interpretation=interpretation)
            return OutlineResult(nodes=nodes, interpretation=interpretation)
        logger.warning("outline_empty", attempt=attempt)
        return OutlineResult(
            nodes=[], interpretation=interpretation, failure_reason="模型返回的大纲是空的"
        )
    return OutlineResult(nodes=[], failure_reason=_reply_excerpt(reply))


def _outline_nodes(data: dict[str, object]) -> list[OutlineNode]:
    """把模型输出里的 outline 数组转成节点，缺 topic 的条目跳过。"""
    nodes: list[OutlineNode] = []
    entries = data.get("outline") or []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        topic = str(entry.get("topic", "")).strip()
        if not topic:
            continue
        nodes.append(
            OutlineNode(
                topic=topic,
                subtopics=[
                    str(s).strip() for s in entry.get("subtopics", []) or [] if str(s).strip()
                ],
                importance=str(entry.get("importance", "common")).strip() or "common",
            )
        )
    return nodes


def _reply_excerpt(reply: str, limit: int = 120) -> str:
    """模型原话的摘录，放进错误信息里给用户看。"""
    text = " ".join(reply.split())
    if not text:
        return "模型没有返回任何内容"
    return f"模型回复：{text[:limit]}{'…' if len(text) > limit else ''}"


# -- 内部工具 -------------------------------------------------------------


async def _correction_samples(
    database: Database, space_id: str, limit: int
) -> list[CorrectionSample]:
    """把纠错反馈还原成出题原料。"""
    # 取该 Space 所有 correction/edit 反馈对应的轨迹
    traces, _total, _cursor = await database.traces.list_by_space(space_id, limit=200)
    samples: list[CorrectionSample] = []
    for trace in traces:
        feedback = await database.feedback.list_by_trace(trace.id)
        correction = next(
            (f for f in feedback if f.kind in ("correction", "edit") and f.comment),
            None,
        )
        if correction is None or not correction.comment:
            continue
        message = await database.messages.get(trace.message_id)
        samples.append(
            CorrectionSample(
                question=trace.query,
                wrong_answer=message.content if message else "",
                correction=correction.comment,
            )
        )
        if len(samples) >= limit:
            break
    return samples


async def _persist_items(
    database: Database, space_id: str, raw: str, source: str
) -> list[EvalItem]:
    """解析出题产出并落库。脏输出不致命：解析失败即本批 0 题。"""
    try:
        data = extract_json(raw)
    except ValueError as exc:
        logger.warning("evalgen_parse_failed", error=str(exc), raw=raw[:200])
        return []
    created: list[EvalItem] = []
    for entry in data.get("items", []) or []:
        question = str(entry.get("question", "")).strip()
        if not question:
            continue
        item = await database.eval_items.create(
            EvalItemCreate(
                space_id=space_id,
                question=question,
                reference=(str(entry.get("reference")).strip() or None)
                if entry.get("reference")
                else None,
                must_include=[
                    str(m).strip() for m in entry.get("must_include", []) or [] if str(m).strip()
                ],
                tags=[str(t).strip() for t in entry.get("tags", []) or [] if str(t).strip()],
                source=cast(EvalSource, source),
            )
        )
        created.append(item)
    return created


async def _recent_documents(database: Database, space_id: str) -> list[str]:
    """最近入库、且已经切好片的文档 id（新的在前）。"""
    documents, _total, _cursor = await database.documents.list_by_space(
        space_id, status="ready", limit=DEFAULT_DOCUMENT_LIMIT
    )
    return [document.id for document in documents]
