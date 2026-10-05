"""按篇并发地跑摄取流水线，边跑边出结果。

重建索引与批量重试都是「一批文档各跑一遍同一条流水线」。此前它们是一篇接一篇跑的：
一篇文档的耗时几乎全花在等模型（解析后的向量化、抽取），CPU 与磁盘大部分时间闲着，
而等待是可以重叠的。

并发上限沿用 ``ingest_concurrency``（投喂那条路径已经在用它）：再高就轮到向量表与
模型服务被压垮，那时总时长反而变长。

结果**按完成顺序**产出而不是按输入顺序：进度事件本来就带着 document_id，界面按 id
更新即可；攒齐再一起报会让进度条在最后一刻突然跳完。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass

import structlog

from .pipeline import IngestPipeline

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class DocumentOutcome:
    """一篇文档的结果。失败不抛出——一篇挂掉不该中断整批。"""

    document_id: str
    ok: bool
    error: str | None = None


async def run_documents(
    pipeline: IngestPipeline,
    document_ids: Sequence[str],
    *,
    stages: Sequence[str] | None = None,
    concurrency: int = 2,
) -> AsyncGenerator[DocumentOutcome, None]:
    """并发跑完这些文档，谁先完成先产出谁。

    Args:
        pipeline: 摄取流水线。
        document_ids: 要跑的文档。
        stages: 只跑其中几个阶段（例如重建索引只要 chunking + embedding）。
        concurrency: 同时在跑的文档数上限。

    Yields:
        每篇一条 :class:`DocumentOutcome`。

    返回的是异步生成器而不是普通迭代器：调用方提前 ``aclose()``（SSE 被客户端断开时
    就是这条路）要能把剩下的任务收掉，这是契约的一部分。
    """
    if not document_ids:
        return

    async def run_one(document_id: str) -> DocumentOutcome:
        try:
            await pipeline.run(document_id, stages=stages)
        except Exception as exc:
            logger.warning("ingest_document_failed", document_id=document_id, error=str(exc))
            return DocumentOutcome(document_id=document_id, ok=False, error=str(exc))
        return DocumentOutcome(document_id=document_id, ok=True)

    documents = iter(document_ids)
    tasks: set[asyncio.Task[DocumentOutcome]] = set()

    def schedule() -> None:
        document_id = next(documents, None)
        if document_id is not None:
            tasks.add(asyncio.create_task(run_one(document_id), name=f"batch:{document_id}"))

    try:
        for _ in range(min(max(1, concurrency), len(document_ids))):
            schedule()
        while tasks:
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                tasks.remove(task)
                yield task.result()
                schedule()
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
