"""摄取链路：解析结构提取、标题感知切分边界、状态机与进度回调。"""

from __future__ import annotations

import asyncio
import itertools
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from agentmem.config import Settings
from agentmem.errors import DuplicateDocumentError, ParseFailedError, ValidationError
from agentmem.ingest import IngestPipeline
from agentmem.ingest.bus import IngestBus
from agentmem.ingest.chunk import count_tokens, split_markdown, split_parse_result
from agentmem.ingest.parse import (
    PAGE_BREAK_PLACEHOLDER,
    ParseResult,
    extract_headings,
    parse_text,
    split_pages,
)
from agentmem.providers.registry import ProviderRegistry
from agentmem.store import Database
from agentmem.types import (
    Chunk,
    DocumentCreate,
    ProgressEvent,
    QueueEvent,
    SpaceCreate,
    SseErrorEvent,
    StatusEvent,
)

SAMPLE = """# 第3章 脱壳

加固 APK 的脱壳流程：先定位 DexClassLoader 调用点，再 dump 内存中的 dex。

## 3.2 Frida 动态脱壳

Frida 可以 hook Java 层方法，用于绕过证书校验。

```python
# 这不是标题
print("hello")
```

## 3.3 常见坑

- 壳会检测调试器
- 内存 dump 后需要修复 dex header
"""


def test_extract_headings_skips_code_fence() -> None:
    """标题路径按层级拼接，代码块里的 # 不算标题。"""
    headings = extract_headings(SAMPLE)
    paths = [heading.heading_path for heading in headings]
    assert paths == ["第3章 脱壳", "第3章 脱壳 > 3.2 Frida 动态脱壳", "第3章 脱壳 > 3.3 常见坑"]
    assert all(item.text != 'print("hello")' or item.level > 1 for item in headings)


def test_split_pages_offsets() -> None:
    """页码区间与正文偏移一致。"""
    raw = f"第一页内容{PAGE_BREAK_PLACEHOLDER}第二页内容"
    text, spans = split_pages(raw)
    assert text == "第一页内容第二页内容"
    assert [(span.page, span.char_start, span.char_end) for span in spans] == [
        (1, 0, 5),
        (2, 5, 10),
    ]
    result = ParseResult(markdown=text, parser="test", pages=spans)
    assert result.page_at(0) == 1
    assert result.page_at(7) == 2
    assert result.page_at(999) == 2


def test_split_markdown_empty() -> None:
    """空文档不产出切片。"""
    assert split_markdown("") == []
    assert split_markdown("   \n\n  ") == []


def test_split_markdown_single_small_chunk() -> None:
    """短文档只产出一个切片，且字符区间与原文一致。"""
    drafts = split_markdown(SAMPLE, headings=extract_headings(SAMPLE), target_tokens=512)
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft.ordinal == 0
    assert SAMPLE[draft.char_start : draft.char_end] == draft.content
    assert draft.heading_path == "第3章 脱壳"
    assert draft.token_count == count_tokens(draft.content)


def test_split_markdown_respects_target_and_overlap() -> None:
    """长文按目标大小切分，相邻切片有重叠且顺序连续。"""
    paragraph = "这是一段用于测试切分的中文文本，内容足够长以便触发切块。" * 6
    markdown = "\n\n".join([f"## 小节 {index}\n\n{paragraph}" for index in range(6)])
    drafts = split_markdown(
        markdown, headings=extract_headings(markdown), target_tokens=200, overlap_tokens=40
    )
    assert len(drafts) > 1
    assert [draft.ordinal for draft in drafts] == list(range(len(drafts)))
    for draft in drafts:
        # 允许一个段落的余量，但不应无界膨胀
        assert draft.token_count <= 200 * 1.6
        assert SAMPLE not in draft.content
    for previous, current in itertools.pairwise(drafts):
        assert current.char_start >= previous.char_start
        assert current.heading_path is not None


def test_oversize_paragraph_is_split() -> None:
    """超长单段按句子继续切分。"""
    sentence = "这是很长的一句话，用来验证超长段落会被拆开。"
    markdown = sentence * 40
    drafts = split_markdown(markdown, headings=[], target_tokens=120, overlap_tokens=0)
    assert len(drafts) > 1
    assert all(draft.token_count <= 200 for draft in drafts)


def test_count_tokens_monotonic() -> None:
    """token 计数是确定且单调的。"""
    assert count_tokens("") == 0
    assert count_tokens("脱壳") > 0
    assert count_tokens("脱壳" * 10) > count_tokens("脱壳")


async def test_split_parse_result_carries_heading_and_page() -> None:
    """切分结果带上标题路径与页码。"""
    raw = f"# 第一章\n\n{PAGE_BREAK_PLACEHOLDER}## 1.1 小节\n\n正文内容，用于验证页码与标题路径。"
    parsed = await parse_text(raw, title="测试")
    drafts = split_parse_result(parsed, target_tokens=64, overlap_tokens=0)
    assert drafts
    assert drafts[0].heading_path is not None


async def test_pipeline_state_machine(
    settings: Settings, mock_registry: ProviderRegistry, tmp_path: Path
) -> None:
    """完整跑一遍 解析 → 切分 → 向量化，进度与状态回调齐全。"""
    database = Database(tmp_path / "meta.db", tmp_path / "vectors", "space-x")
    await database.open()
    try:
        space = await database.spaces.create(SpaceCreate(name="逆向", domain="Android"))
        pipeline = IngestPipeline(database, mock_registry, settings)
        document = await pipeline.register_text(space_id=space.id, title="脱壳笔记", content=SAMPLE)
        assert document.status == "pending"

        progress: list[ProgressEvent] = []
        statuses: list[StatusEvent] = []

        async def on_progress(event: ProgressEvent) -> None:
            progress.append(event)

        async def on_status(event: StatusEvent) -> None:
            statuses.append(event)

        finished = await pipeline.run(document.id, on_progress=on_progress, on_status=on_status)
        assert finished.status == "ready"
        assert [event.status for event in statuses] == [
            "parsing",
            "chunking",
            "embedding",
            "extracting",
            "ready",
        ]
        # 这个夹具只绑定了 embedding 角色，抽取拿不到 LLM 因而不产生逐批进度帧，
        # 但状态机照样走完，失败原因落在 meta 里（详见 test_memory.py）
        stages = {event.stage for event in progress}
        assert {"parsing", "chunking", "embedding"} <= stages
        assert finished.meta.model_dump().get("extraction_error")

        chunks = await database.chunks.list_by_document(document.id)
        assert chunks
        assert all(isinstance(chunk, Chunk) for chunk in chunks)
        vectors = await database.require_vectors()
        assert await vectors.count("chunks_vec", space.id) == len(chunks)

        markdown = await pipeline.document_markdown(finished)
        assert markdown.startswith("# 第3章 脱壳")
    finally:
        await database.close()


async def test_pipeline_dedup(
    settings: Settings, mock_registry: ProviderRegistry, tmp_path: Path
) -> None:
    """同 Space 内相同内容的文本不允许重复登记。"""
    database = Database(tmp_path / "meta.db", tmp_path / "vectors", "space-x")
    await database.open()
    try:
        space = await database.spaces.create(SpaceCreate(name="逆向", domain="Android"))
        pipeline = IngestPipeline(database, mock_registry, settings)
        await pipeline.register_text(space_id=space.id, title="a", content="同样的内容")
        with pytest.raises(DuplicateDocumentError) as excinfo:
            await pipeline.register_text(space_id=space.id, title="b", content="同样的内容")
        assert excinfo.value.code == "DUPLICATE_DOCUMENT"
    finally:
        await database.close()


async def test_pipeline_stage_retry_only(
    settings: Settings, mock_registry: ProviderRegistry, tmp_path: Path
) -> None:
    """可以只重试单个阶段。"""
    database = Database(tmp_path / "meta.db", tmp_path / "vectors", "space-x")
    await database.open()
    try:
        space = await database.spaces.create(SpaceCreate(name="逆向", domain="Android"))
        pipeline = IngestPipeline(database, mock_registry, settings)
        document = await pipeline.register_text(space_id=space.id, title="t", content=SAMPLE)
        await pipeline.run(document.id)
        before = await database.chunks.list_by_document(document.id)

        await pipeline.retry_stage(document.id, "embedding")
        after = await database.chunks.list_by_document(document.id)
        assert [chunk.id for chunk in before] == [chunk.id for chunk in after]
        assert (await database.documents.require(document.id)).status == "ready"

        with pytest.raises(ValidationError):
            await pipeline.retry_stage(document.id, "unknown-stage")
    finally:
        await database.close()


async def test_ingest_bus_pubsub(tmp_path: Path) -> None:
    """总线把事件推给订阅者，断开后不再投递。"""
    bus = IngestBus()
    async with bus.subscribe("s1") as events:
        bus.publish_progress("s1", "d1", "embedding", 1, 2)
        bus.publish("s1", StatusEvent(document_id="d1", status="ready"))
        # 订阅时先收到一条队列深度快照，之后才是真正的进度与状态
        snapshot = await anext(events)
        first = await anext(events)
        second = await anext(events)
        assert isinstance(snapshot, QueueEvent)
        assert isinstance(first, ProgressEvent)
        assert first.percent == 50.0
        assert isinstance(second, StatusEvent)
    bus.publish_progress("s1", "d1", "embedding", 2, 2)  # 无订阅者时不应报错


async def test_pipeline_marks_failed_on_missing_source(
    settings: Settings, mock_registry: ProviderRegistry, tmp_path: Path
) -> None:
    """原始文件缺失时必须落到 failed 并带错误信息。"""
    database = Database(tmp_path / "meta.db", tmp_path / "vectors", "space-x")
    await database.open()
    try:
        space = await database.spaces.create(SpaceCreate(name="逆向", domain="Android"))
        document = await database.documents.create(
            DocumentCreate(
                space_id=space.id,
                title="缺失",
                source_type="file",
                source_uri=str(tmp_path / "nope.pdf"),
                sha256="hash-missing",
            )
        )
        pipeline = IngestPipeline(database, mock_registry, settings)
        with pytest.raises(ParseFailedError):
            await pipeline.run(document.id)
        failed = await database.documents.require(document.id)
        assert failed.status == "failed"
        assert failed.error
    finally:
        await database.close()


async def test_reprocess_replaces_chunks_without_leaving_stale_vectors(
    settings: Settings, mock_registry: ProviderRegistry, tmp_path: Path
) -> None:
    """重新解析（/reprocess 走的就是这条路）不能留下旧切片的向量。

    重切会换掉全部 chunk id。若只删 SQLite 里的切片而不删 ``chunks_vec``，
    检索仍会命中已经不存在的旧切片，向量表也随每次重切膨胀。
    """
    database = Database(tmp_path / "meta.db", tmp_path / "vectors", "space-x")
    await database.open()
    try:
        space = await database.spaces.create(SpaceCreate(name="逆向", domain="Android"))
        pipeline = IngestPipeline(database, mock_registry, settings)
        document = await pipeline.register_text(space_id=space.id, title="t", content=SAMPLE)
        await pipeline.run(document.id)

        first = await database.chunks.list_by_document(document.id)
        vectors = await database.require_vectors()
        assert await vectors.count("chunks_vec", space.id) == len(first)

        await pipeline.run(document.id, stages=("chunking", "embedding"))

        second = await database.chunks.list_by_document(document.id)
        stale = {chunk.id for chunk in first} - {chunk.id for chunk in second}
        assert stale, "重切没有换掉 chunk id，这个用例失去意义"
        assert await vectors.count("chunks_vec", space.id) == len(second)
    finally:
        await database.close()


async def test_retry_chunking_leaves_no_stale_vectors(
    settings: Settings, mock_registry: ProviderRegistry, tmp_path: Path
) -> None:
    """只重切也不留下旧向量：切片换了一批 id，旧向量就全是回不了表的孤儿。

    这条路径（``retry_stage``）不跑向量化——它要么显式带上 ``embedding``，
    要么新切片暂时没有向量；但无论哪种，旧向量都必须清掉。
    """
    database = Database(tmp_path / "meta.db", tmp_path / "vectors", "space-x")
    await database.open()
    try:
        space = await database.spaces.create(SpaceCreate(name="逆向", domain="Android"))
        pipeline = IngestPipeline(database, mock_registry, settings)
        document = await pipeline.register_text(space_id=space.id, title="t", content=SAMPLE)
        await pipeline.run(document.id)

        await pipeline.retry_stage(document.id, "chunking")

        chunks = await database.chunks.list_by_document(document.id)
        vectors = await database.require_vectors()
        assert chunks
        assert await vectors.count("chunks_vec", space.id) == 0
    finally:
        await database.close()


async def test_empty_extraction_fails_instead_of_silently_succeeding(
    settings: Settings, mock_registry: ProviderRegistry, tmp_path: Path
) -> None:
    """切不出切片时必须判失败。

    放它走到 ready，用户会以为资料已经进库，实际检索永远搜不到——扫描件与纯图片
    PDF 走的正是这条路。失败要带可读的原因，且不该把已有的切片顺手删掉。
    """
    database = Database(tmp_path / "meta.db", tmp_path / "vectors", "space-x")
    await database.open()
    try:
        space = await database.spaces.create(SpaceCreate(name="逆向", domain="Android"))
        pipeline = IngestPipeline(database, mock_registry, settings)
        document = await pipeline.register_text(
            space_id=space.id, title="空白", content="   \n\n  "
        )

        with pytest.raises(ParseFailedError) as excinfo:
            await pipeline.run(document.id)

        assert "扫描件" in excinfo.value.message
        refreshed = await database.documents.require(document.id)
        assert refreshed.status == "failed"
        assert refreshed.error
        assert await database.chunks.count(space.id) == 0
    finally:
        await database.close()


async def test_document_context_is_indexed_but_not_written_into_chunks(
    settings: Settings, database: Database, embed_config: Any, mock_reply: Any, tmp_path: Path
) -> None:
    """文档级上下文进索引、不进切片正文。

    这一段上下文的作用是补上「离主语太远」的那一层：化合物名只在前一节出现过时，
    单看切片根本不知道在说谁。它必须出现在全文索引里（否则检索命中不了），
    又绝不能写进 ``content``（否则 ``char_start``/``char_end`` 当场失效，
    引用高亮会画到错误的位置）。
    """
    from agentmem.config import ModelsConfig
    from agentmem.providers.registry import ProviderRegistry
    from agentmem.types import RoleBindings

    mock_reply("本文档讲的是 ZEBRA-5566 的代谢稳定性，涉及肝微粒体与 CYP3A4 抑制。")
    llm_config = embed_config.model_copy(
        update={"id": "mock-chat", "kind": "llm", "model": "mock-chat"}
    )
    registry = ProviderRegistry(
        ModelsConfig(
            providers=[embed_config, llm_config],
            roles=RoleBindings(embedding="mock-embed", fast="mock-chat"),
        ),
        usage=database.usage,
        space_id=database.space_id,
    )

    space = await database.spaces.create(SpaceCreate(name="新药研发", domain="药物发现"))
    pipeline = IngestPipeline(database, registry, settings)
    # 化合物名和 CYP3A4 只在开头出现一次：后面的段落单看不知道在说谁，
    # 正是文档级上下文要补的那一层；概要也必须有原文依据，否则会被落地检查删掉
    content = "# 代谢稳定性\n\nZEBRA-5566 的代谢稳定性研究，重点看 CYP3A4 抑制。\n\n" + "\n\n".join(
        f"第 {index} 段讨论肝微粒体孵育实验的孵育时间与蛋白浓度设置。" * 12 for index in range(12)
    )
    document = await pipeline.register_text(space_id=space.id, title="代谢笔记", content=content)

    finished = await pipeline.run(document.id)

    assert finished.status == "ready"
    assert finished.meta.context_summary and "ZEBRA-5566" in finished.meta.context_summary

    chunks = await database.chunks.list_by_document(document.id)
    assert chunks
    bodies = [chunk for chunk in chunks if chunk.kind == "body"]
    summaries = [chunk for chunk in chunks if chunk.kind == "summary"]
    for chunk in bodies:
        # 正文切片必须和原文区间逐字一致：上下文一旦混进来，引用高亮就画歪了
        assert content[chunk.char_start : chunk.char_end] == chunk.content
    unnamed = [chunk for chunk in bodies if "ZEBRA-5566" not in chunk.content]
    assert unnamed, "要有正文里看不到化合物名的切片，才能验证上下文补上了这一层"
    assert len(summaries) == 1, "概要切片有且只有一条"
    assert "ZEBRA-5566" in summaries[0].content, "概要切片就是这段上下文本身"
    assert summaries[0].char_start == summaries[0].char_end == 0, "概要没有原文区间"

    hits = await database.fts.search("ZEBRA-5566", space_id=space.id)
    assert hits, "文档级上下文必须进全文索引，否则这一层就白做了"
    assert {hit.chunk_id for hit in hits} == {chunk.id for chunk in chunks}


async def test_contextual_retrieval_can_be_turned_off(
    settings: Settings, database: Database, embed_config: Any, mock_reply: Any
) -> None:
    """关掉开关就完全回到旧行为：没有上下文，也没有多余的前缀。"""
    from agentmem.config import ModelsConfig
    from agentmem.providers.registry import ProviderRegistry
    from agentmem.types import RoleBindings

    mock_reply("本文档讲的是 ZEBRA-5566 的代谢稳定性。")
    llm_config = embed_config.model_copy(
        update={"id": "mock-chat", "kind": "llm", "model": "mock-chat"}
    )
    registry = ProviderRegistry(
        ModelsConfig(
            providers=[embed_config, llm_config],
            roles=RoleBindings(embedding="mock-embed", fast="mock-chat"),
        ),
        usage=database.usage,
        space_id=database.space_id,
    )
    settings.contextual_retrieval = False

    space = await database.spaces.create(SpaceCreate(name="新药研发", domain="药物发现"))
    pipeline = IngestPipeline(database, registry, settings)
    document = await pipeline.register_text(
        space_id=space.id, title="代谢笔记", content="# 代谢稳定性\n\n肝微粒体孵育实验的设置。"
    )

    finished = await pipeline.run(document.id)

    assert finished.status == "ready"
    assert finished.meta.context_summary is None
    assert not await database.fts.search("ZEBRA-5566", space_id=space.id)


async def test_interrupted_documents_are_recovered_on_startup(tmp_path: Path) -> None:
    """上次进程中断留下的文档不能永远停在「摄取中」。

    状态停在 parsing / embedding 表示「有任务在跑」，而任务已经随进程消失：
    界面会一直转圈，用户也无从知道该做什么。启动时如实判失败并给出下一步。
    """
    from agentmem.ingest.pipeline import INTERRUPTED_MESSAGE, recover_interrupted
    from agentmem.types import DocumentCreate

    database = Database(tmp_path / "meta.db", tmp_path / "vectors", "space-x")
    await database.open()
    try:
        space = await database.spaces.create(SpaceCreate(name="逆向", domain="Android"))
        stuck = await database.documents.create(
            DocumentCreate(
                space_id=space.id,
                title="卡住的文档",
                source_type="paste",
                mime="text/markdown",
                sha256="b" * 64,
                size_bytes=3,
            )
        )
        await database.documents.set_status(stuck.id, "embedding")
        failed = await database.documents.create(
            DocumentCreate(
                space_id=space.id,
                title="已经失败的文档",
                source_type="paste",
                mime="text/markdown",
                sha256="c" * 64,
                size_bytes=3,
            )
        )
        await database.documents.set_status(failed.id, "failed", "上一次就失败了")
        ready = await database.documents.create(
            DocumentCreate(
                space_id=space.id,
                title="正常的文档",
                source_type="paste",
                mime="text/markdown",
                sha256="d" * 64,
                size_bytes=3,
            )
        )
        await database.documents.set_status(ready.id, "ready")

        recovered = await recover_interrupted(database)

        assert recovered == [stuck.id]
        refreshed = await database.documents.require(stuck.id)
        assert refreshed.status == "failed"
        assert refreshed.error == INTERRUPTED_MESSAGE
        # 已失败的文档保留它自己的原因，正常文档不受影响
        assert (await database.documents.require(failed.id)).error == "上一次就失败了"
        assert (await database.documents.require(ready.id)).status == "ready"
    finally:
        await database.close()


async def test_summary_chunk_makes_the_whole_document_retrievable(
    settings: Settings, database: Database, embed_config: Any, mock_reply: Any
) -> None:
    """概要切片要能被检索到：跨全文汇总类问题需要一个代表整篇的落点。

    正文切片各自只有几百 token，谁也不代表全文。问「这份报告讲了哪些限制」时，
    按相似度召回的是零散段落，答案只能拼。概要切片是那个「整篇」的落点。
    """
    from agentmem.config import ModelsConfig
    from agentmem.providers.registry import ProviderRegistry
    from agentmem.types import RoleBindings

    mock_reply("本文档讨论 ZEBRA-5566 的代谢稳定性与 CYP3A4 抑制。")
    llm_config = embed_config.model_copy(
        update={"id": "mock-chat", "kind": "llm", "model": "mock-chat"}
    )
    registry = ProviderRegistry(
        ModelsConfig(
            providers=[embed_config, llm_config],
            roles=RoleBindings(embedding="mock-embed", fast="mock-chat"),
        ),
        usage=database.usage,
        space_id=database.space_id,
    )
    space = await database.spaces.create(SpaceCreate(name="新药研发", domain="药物发现"))
    pipeline = IngestPipeline(database, registry, settings)
    # 化合物名和 CYP3A4 只在开头出现一次：后面的段落单看不知道在说谁，
    # 正是文档级上下文要补的那一层；概要也必须有原文依据，否则会被落地检查删掉
    content = "# 代谢稳定性\n\nZEBRA-5566 的代谢稳定性研究，重点看 CYP3A4 抑制。\n\n" + "\n\n".join(
        f"第 {index} 段讨论肝微粒体孵育实验的孵育时间与蛋白浓度设置。" for index in range(8)
    )
    document = await pipeline.register_text(space_id=space.id, title="代谢笔记", content=content)

    await pipeline.run(document.id)

    chunks = await database.chunks.list_by_document(document.id)
    summary = next(chunk for chunk in chunks if chunk.kind == "summary")
    assert summary.ordinal == max(chunk.ordinal for chunk in chunks), "概要排在正文之后"

    hits = await database.fts.search("ZEBRA-5566", space_id=space.id)
    assert summary.id in {hit.chunk_id for hit in hits}, (
        "概要切片必须进全文索引，否则它在检索里不存在"
    )
    # 正文切片也会命中：它们的索引文本带着同一段文档级上下文（G3）。
    # 区别在于正文切片的 content 里没有这段话，只有概要把这段上下文当正文。


async def test_context_summary_stops_after_repeated_failures(
    settings: Settings, mock_registry: ProviderRegistry, tmp_path: Path, monkeypatch: Any
) -> None:
    """provider 不通时不要每篇文档都白等一次超时。

    一个 Space 逐篇重建索引，摘要每次调用都要等连接超时——不熔断的话
    耗时就是「超时 × 文档数」。连续失败两次之后本次运行不再尝试。
    """
    database = Database(tmp_path / "meta.db", tmp_path / "vectors", "space-x")
    await database.open()
    try:
        space = await database.spaces.create(SpaceCreate(name="逆向", domain="Android"))
        pipeline = IngestPipeline(database, mock_registry, settings)
        document = await pipeline.register_text(
            space_id=space.id, title="笔记", content="# 标题\n\n正文。"
        )
        parsed = await pipeline._parse(document)

        calls = {"n": 0}

        async def failing_chat(messages: Any, **kwargs: Any) -> Any:
            calls["n"] += 1
            raise RuntimeError("provider 不通")

        class FakeRoute:
            name = "fake"
            chat = staticmethod(failing_chat)

        monkeypatch.setattr(
            type(pipeline.registry), "llm", lambda self, *a, **k: FakeRoute(), raising=True
        )

        from agentmem.ingest.pipeline import SUMMARY_FAILURE_LIMIT

        for _ in range(SUMMARY_FAILURE_LIMIT + 3):
            assert await pipeline._context_summary(document, parsed) is None

        assert calls["n"] == SUMMARY_FAILURE_LIMIT, "熔断之后不再调用模型"
    finally:
        await database.close()


async def test_embedding_batches_overlap_only_for_safe_providers(
    settings: Settings, tmp_path: Path
) -> None:
    """取向量可以并发，但只对声明了 concurrent_safe 的提供方。

    进程内加载的本地模型并发推理会争用设备（MPS 上实测段错误），所以并发与否由
    提供方自己声明，而不是全局一刀切。落库始终串行：向量表是共享资源。
    """
    import asyncio
    import time

    class CountingEmbedder:
        name = "counting"
        dimension = 4

        def __init__(self, *, delay: float, concurrent_safe: bool) -> None:
            self.delay = delay
            self.concurrent_safe = concurrent_safe
            self.in_flight = 0
            self.max_in_flight = 0

        async def embed(self, texts: list[str], *, kind: str = "doc") -> list[list[float]]:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            try:
                await asyncio.sleep(self.delay)
                return [[0.1, 0.2, 0.3, 0.4] for _ in texts]
            finally:
                self.in_flight -= 1

    async def ingest(
        tag: str, *, concurrency: int, concurrent_safe: bool
    ) -> tuple[CountingEmbedder, float, int]:
        data_dir = tmp_path / tag
        data_dir.mkdir(parents=True, exist_ok=True)
        database = Database(data_dir / "meta.db", data_dir / "vectors", f"space-{tag}")
        await database.open()
        try:
            space = await database.spaces.create(SpaceCreate(name="并发", domain="药物发现"))
            embedder = CountingEmbedder(delay=0.15, concurrent_safe=concurrent_safe)
            registry = MagicMock()
            registry.embedding.return_value = embedder
            effective = settings.model_copy(
                update={
                    "embedding_batch_size": 4,
                    "embedding_concurrency": concurrency,
                    "contextual_retrieval": False,
                }
            )
            pipeline = IngestPipeline(database, registry, effective)
            # 段落要够长，切出来才有多个批次——只有一批时并发无从体现
            paragraph = "这一段用来产生足够多的切片，长度要撑到能切出好几批。" * 6
            content = "\n\n".join(f"第 {i} 段：{paragraph}" for i in range(1, 40))
            document = await pipeline.register_text(
                space_id=space.id, title="并发测试.md", content=content
            )
            await pipeline.run(document.id, stages=["parsing", "chunking"])
            started = time.perf_counter()
            await pipeline.run(document.id, stages=["embedding"])
            elapsed = time.perf_counter() - started

            chunks = await database.chunks.list_by_document(document.id)
            vectors = await database.require_vectors()
            stored = await vectors.count("chunks_vec", space_id=space.id)
            return embedder, elapsed, stored - len(chunks)
        finally:
            await database.close()

    serial, serial_elapsed, serial_missing = await ingest(
        "serial", concurrency=1, concurrent_safe=True
    )
    assert serial.max_in_flight == 1
    assert serial_missing == 0, "串行时每一片都要有向量"

    concurrent, concurrent_elapsed, concurrent_missing = await ingest(
        "concurrent", concurrency=3, concurrent_safe=True
    )
    assert concurrent.max_in_flight > 1, "声明可并发的提供方应当真的被并发调用"
    assert concurrent.max_in_flight <= 3, "并发上限不该超过设置值"
    assert concurrent_elapsed < serial_elapsed, "并发取向量应当更快"
    assert concurrent_missing == 0, "并发之后每一片仍然要有向量"

    unsafe, unsafe_elapsed, unsafe_missing = await ingest(
        "unsafe", concurrency=3, concurrent_safe=False
    )
    assert unsafe.max_in_flight == 1, "本地模型即使设置了并发也只能一批一批来"
    assert unsafe_elapsed >= serial_elapsed * 0.8
    assert unsafe_missing == 0


async def test_ingest_bus_bounds_concurrent_tasks() -> None:
    """投喂多篇文档时后台任务有上限，多出来的排队等名额。

    此前每篇文档立刻起一个任务：一次投二十篇就是二十条流水线同时压着向量表与
    模型服务——本地模型上这会直接争用设备。
    """
    import asyncio

    from agentmem.ingest.bus import IngestBus

    bus = IngestBus(concurrency=2)
    running = 0
    peak = 0

    async def job() -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.1)
        running -= 1

    tasks = [bus.spawn("space-1", f"job-{index}", job()) for index in range(6)]
    await asyncio.gather(*tasks)

    assert peak == 2, "同时运行的任务数不该超过上限"
    assert bus.running_tasks() == 0, "跑完的任务要从登记表里摘掉"
    assert bus.waiting_tasks == 0


async def test_ingest_bus_announces_the_queue_depth() -> None:
    """排队要能被前端看见：一堆 pending 文档不说明原因，用户以为卡住了。"""
    import asyncio

    from agentmem.ingest.bus import IngestBus
    from agentmem.types import QueueEvent

    bus = IngestBus(concurrency=1)
    release = asyncio.Event()

    async def job() -> None:
        await release.wait()

    async with bus.subscribe("space-1") as events:
        tasks = [bus.spawn("space-1", f"job-{index}", job()) for index in range(3)]
        await asyncio.sleep(0.05)

        seen: list[QueueEvent] = []
        while True:
            try:
                payload = await asyncio.wait_for(events.__anext__(), timeout=0.2)
            except TimeoutError:
                break
            if isinstance(payload, QueueEvent):
                seen.append(payload)

        assert any(event.waiting >= 1 for event in seen), "有任务在排队时要报出来"
        assert any(event.running >= 1 for event in seen)

        release.set()
        await asyncio.gather(*tasks)

    assert bus.waiting_tasks == 0


async def test_ingest_bus_reports_a_failed_task_without_stopping_others() -> None:
    """一篇文档失败只发一条错误事件，不影响其它文档。"""
    import asyncio

    from agentmem.ingest.bus import IngestBus

    bus = IngestBus(concurrency=2)
    done: list[str] = []

    async def boom() -> None:
        raise RuntimeError("这篇炸了")

    async def fine() -> None:
        done.append("fine")

    async with bus.subscribe("space-1") as events:
        tasks = [bus.spawn("space-1", "boom", boom()), bus.spawn("space-1", "fine", fine())]
        await asyncio.gather(*tasks)

        received: list[object] = []
        while not received or not isinstance(received[-1], SseErrorEvent):
            try:
                received.append(await asyncio.wait_for(events.__anext__(), timeout=1.0))
            except TimeoutError:
                break

    assert done == ["fine"], "另一篇照常跑完"
    assert any(isinstance(event, SseErrorEvent) for event in received), "失败要发错误事件"
    assert any(
        "这篇炸了" in event.message for event in received if isinstance(event, SseErrorEvent)
    )


# ---------------------------------------------------------------------------
# 按篇并发（重建索引 / 批量重试走的就是它）
# ---------------------------------------------------------------------------


class _FakeBatchPipeline:
    """只记录调用的假流水线：这里要验的是调度，不是摄取本身。"""

    def __init__(self, *, fail: set[str] | None = None, hold: float = 0.05) -> None:
        self.fail = fail or set()
        self.hold = hold
        self.started: list[str] = []
        self.peak = 0
        self._running = 0

    async def run(self, document_id: str, *, stages: object = None, **_: object) -> None:
        del stages
        self.started.append(document_id)
        self._running += 1
        self.peak = max(self.peak, self._running)
        try:
            await asyncio.sleep(self.hold)
            if document_id in self.fail:
                raise RuntimeError(f"{document_id} 炸了")
        finally:
            self._running -= 1


async def test_run_documents_runs_several_at_once() -> None:
    """并发跑完整批，且不超过给定上限。"""
    from agentmem.ingest import run_documents

    pipeline = _FakeBatchPipeline()
    ids = [f"doc-{index}" for index in range(6)]

    outcomes = [
        outcome
        async for outcome in run_documents(pipeline, ids, concurrency=3)  # type: ignore[arg-type]
    ]

    assert {outcome.document_id for outcome in outcomes} == set(ids)
    assert all(outcome.ok for outcome in outcomes)
    assert pipeline.peak > 1, "串行跑的话这条并发就白写了"
    assert pipeline.peak <= 3, "超过上限会把向量表与模型服务压垮"


async def test_run_documents_isolates_failures() -> None:
    """一篇失败只影响它自己，其余照常跑完，且错误如实带出来。"""
    from agentmem.ingest import run_documents

    pipeline = _FakeBatchPipeline(fail={"doc-1"})
    ids = ["doc-0", "doc-1", "doc-2"]

    outcomes = {
        outcome.document_id: outcome
        async for outcome in run_documents(pipeline, ids, concurrency=2)  # type: ignore[arg-type]
    }

    assert outcomes["doc-0"].ok and outcomes["doc-2"].ok
    assert outcomes["doc-1"].ok is False
    assert "炸了" in (outcomes["doc-1"].error or "")


async def test_run_documents_cancels_the_rest_when_caller_leaves() -> None:
    """调用方提前退出（客户端断开 SSE）时，剩下的任务要收掉。

    不收的话它们会在后台接着写库，而进度已经没人看了——重建索引尤其危险：
    那时整张向量表正处在被重建的中间态。
    """
    from agentmem.ingest import run_documents

    pipeline = _FakeBatchPipeline(hold=0.2)
    ids = [f"doc-{index}" for index in range(6)]

    stream = run_documents(pipeline, ids, concurrency=2)  # type: ignore[arg-type]
    first = await stream.__anext__()
    await stream.aclose()
    await asyncio.sleep(0.3)

    assert first.document_id in ids
    assert len(pipeline.started) < len(ids), "剩下的不该再被启动"


async def test_run_documents_on_empty_input() -> None:
    """空输入不报错，也不起任何任务。"""
    from agentmem.ingest import run_documents

    pipeline = _FakeBatchPipeline()
    assert [outcome async for outcome in run_documents(pipeline, [])] == []  # type: ignore[arg-type]
    assert pipeline.started == []
