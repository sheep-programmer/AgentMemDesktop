"""比较旧版全量建任务与当前滑动窗口的调度开销，不调用模型或改动用户数据。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import tracemalloc
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from typing import cast

from agentmem.ingest.batch import DocumentOutcome, run_documents
from agentmem.ingest.pipeline import IngestPipeline


class HoldingPipeline:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, document_id: str, *, stages: Sequence[str] | None = None) -> None:
        self.entered.set()
        await self.release.wait()


async def legacy(
    pipeline: HoldingPipeline, ids: list[str], concurrency: int
) -> AsyncGenerator[DocumentOutcome, None]:
    slots = asyncio.Semaphore(concurrency)

    async def work(document_id: str) -> DocumentOutcome:
        async with slots:
            await pipeline.run(document_id)
            return DocumentOutcome(document_id=document_id, ok=True)

    tasks = [asyncio.create_task(work(document_id)) for document_id in ids]
    try:
        for task in asyncio.as_completed(tasks):
            yield await task
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def measure(mode: str, count: int, concurrency: int) -> dict[str, int | float | str]:
    pipeline = HoldingPipeline()
    ids = [f"doc-{index}" for index in range(count)]
    iterator = (
        legacy(pipeline, ids, concurrency)
        if mode == "legacy"
        else run_documents(cast(IngestPipeline, pipeline), ids, concurrency=concurrency)
    )
    before = len(asyncio.all_tasks())
    tracemalloc.start()
    started = time.perf_counter()
    consumer = asyncio.ensure_future(anext(iterator))
    try:
        await pipeline.entered.wait()
        await asyncio.sleep(0)
        elapsed_ms = (time.perf_counter() - started) * 1000
        _current, peak = tracemalloc.get_traced_memory()
        # 排除驱动生成器的一个任务，只统计处理文档的在途任务。
        workers = len(asyncio.all_tasks()) - before - 1
    finally:
        tracemalloc.stop()
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        await iterator.aclose()
    return {
        "mode": mode,
        "documents": count,
        "concurrency": concurrency,
        "live_document_tasks": workers,
        "scheduling_ms": round(elapsed_ms, 3),
        "python_peak_bytes": peak,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--documents", type=int, default=10_000)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.documents < 1 or args.concurrency < 1:
        parser.error("documents 与 concurrency 必须为正整数")
    result = {
        "scope": "Python 调度器分配与在途任务；不包含模型推理、解析、磁盘 I/O 或总摄取耗时",
        "results": [
            await measure("legacy", args.documents, args.concurrency),
            await measure("bounded", args.documents, args.concurrency),
        ],
    }
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    sys.stdout.write(encoded)


if __name__ == "__main__":
    asyncio.run(main())
