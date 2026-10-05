"""回归测试：每条对应一个**实际发生过**的缺陷。

删除任何一条之前，先确认对应的故障模式已经不可能重现。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractContextManager
from pathlib import Path

import pytest

from agentmem.config import Settings
from agentmem.ingest.parse import ParseResult
from agentmem.providers.base import ChatChunk
from agentmem.providers.base import Message as ProviderMessage
from agentmem.providers.registry import ProviderRegistry
from agentmem.store import Database
from agentmem.store.fts import FtsIndex
from agentmem.store.sqlite import SQLiteDatabase, now_ms
from agentmem.types import Chunk, EvalItemScore, Message

# ---------------------------------------------------------------- FTS 并发


@pytest.fixture
async def fts_db(tmp_path: Path) -> AsyncIterator[tuple[SQLiteDatabase, FtsIndex]]:
    """一个装了 200 条切片、已建好全文索引的库。"""
    database = SQLiteDatabase(tmp_path / "t.db")
    await database.connect()
    ts = now_ms()
    await database.execute(
        "INSERT INTO spaces (id,name,domain,created_at,updated_at) VALUES ('sp','s','d',?,?)",
        (ts, ts),
    )
    await database.execute(
        "INSERT INTO documents (id,space_id,title,source_type,sha256,status,created_at,updated_at)"
        " VALUES ('doc','sp','t','paste','h','ready',?,?)",
        (ts, ts),
    )
    for i in range(200):
        await database.execute(
            "INSERT INTO chunks (id,space_id,document_id,ordinal,content,created_at)"
            " VALUES (?,?,?,?,?,?)",
            (f"c{i}", "sp", "doc", i, f"加固 APK 脱壳 分析 第{i}章 DexClassLoader 内存 dump", ts),
        )
    index = FtsIndex(database)
    await index.rebuild()
    try:
        yield database, index
    finally:
        await database.close()


async def test_fts_search_is_concurrency_safe(fts_db: tuple[SQLiteDatabase, FtsIndex]) -> None:
    """并行召回时全文检索必须稳定。

    背景：``FtsIndex`` 曾直接使用底层 ``sqlite3.Connection``，绕过了数据库锁。
    检索管线的向量路与全文路是 ``asyncio.gather`` 并行的，两个线程同时在一个
    连接上跑 FTS5 MATCH 时，轻则 ``bm25()`` 返回 NULL 导致整路召回静默丢失，
    重则直接抛 ``sqlite3.InterfaceError``（实测并发第一轮就复现）。
    """
    _, index = fts_db

    async def search() -> int:
        return len(await index.search("脱壳 加固", limit=50, space_id="sp"))

    expected = await search()
    assert expected > 0

    for _ in range(20):
        results = await asyncio.gather(*(search() for _ in range(4)))
        assert all(count == expected for count in results), (
            f"并发检索结果不一致：{results}，期望全部为 {expected}"
        )


async def test_fts_index_and_search_interleaved(fts_db: tuple[SQLiteDatabase, FtsIndex]) -> None:
    """摄取（写索引）与检索（读索引）并发时不得崩溃。"""
    database, index = fts_db
    ts = now_ms()
    for i in range(200, 240):
        await database.execute(
            "INSERT INTO chunks (id,space_id,document_id,ordinal,content,created_at)"
            " VALUES (?,?,?,?,?,?)",
            (f"n{i}", "sp", "doc", i, f"新增 脱壳 样本 {i}", ts),
        )

    await asyncio.gather(
        index.index_chunks([(f"n{i}", f"新增 脱壳 样本 {i}") for i in range(200, 240)]),
        index.search("脱壳", limit=50, space_id="sp"),
        index.search("加固", limit=50, space_id="sp"),
    )


# ---------------------------------------------------------------- 空白裁剪


def test_streaming_delta_preserves_whitespace() -> None:
    """流式增量**绝不能**被裁剪空白。

    背景：全局 ``str_strip_whitespace=True`` 曾作用到 ``ChatChunk.delta`` 上。
    模型逐 token 吐字时，增量常常就是 ``" is"`` 这种带前导空格的片段，
    裁剪之后 "Frida is a toolkit" 会被拼成 "Fridaisatoolkit"。
    """
    tokens = ["Frida", " is", " a", " dynamic", " instrumentation", " toolkit"]
    joined = "".join(ChatChunk(delta=token).delta for token in tokens)
    assert joined == "Frida is a dynamic instrumentation toolkit"
    assert ChatChunk(delta=" ").delta == " "


def test_content_fields_preserve_whitespace() -> None:
    """正文类字段一律不裁剪：缩进、首尾换行都是有意义的。"""
    code = "def main():\n    return 1\n"
    assert ProviderMessage(role="user", content=code).content == code

    chunk = Chunk(
        id="c1",
        space_id="sp",
        document_id="doc",
        ordinal=0,
        content="    缩进开头的内容    ",
        created_at=now_ms(),
    )
    # 切片正文被裁剪会让 char_start/char_end 偏移错位，引用定位跟着错
    assert chunk.content == "    缩进开头的内容    "

    message = Message(
        id="m1",
        conversation_id="conv",
        role="assistant",
        content="\n首尾换行要保留\n",
        created_at=now_ms(),
    )
    assert message.content == "\n首尾换行要保留\n"


def test_identifier_fields_still_strip() -> None:
    """名称类字段仍应裁剪——这是当初开启该配置的正当理由，不能一并关掉。"""
    from agentmem.types import SpaceCreate

    space = SpaceCreate(name="  逆向  ", domain="  Android  ")
    assert space.name == "逆向"
    assert space.domain == "Android"


# ---------------------------------------------------------------- 解析缓存原子写


async def test_parse_cache_write_is_atomic(tmp_path: Path) -> None:
    """写解析缓存时，并发读取不得看到半截内容。

    背景：``_save_parsed`` 曾以 ``"w"`` 模式直接打开目标文件，该模式会**立即清空**它。
    摄取是后台任务，前端此刻很可能正在读同一个文件，于是读到空内容，
    报 ``Invalid JSON: EOF``（实测约 1/10 概率）。改为「临时文件 + os.replace」后消失。
    """
    target = tmp_path / "doc.parse.json"
    big = ParseResult(markdown="正文" * 20000, parser="paste")
    target.write_text(big.model_dump_json(), encoding="utf-8")

    async def writer() -> None:
        for _ in range(30):
            tmp = target.with_suffix(".tmp")
            tmp.write_text(big.model_dump_json(), encoding="utf-8")
            await asyncio.to_thread(tmp.replace, target)
            await asyncio.sleep(0)

    async def reader() -> None:
        for _ in range(200):
            text = target.read_text(encoding="utf-8")
            # 要么是完整的旧内容，要么是完整的新内容，不存在中间态
            ParseResult.model_validate_json(text)
            await asyncio.sleep(0)

    await asyncio.gather(writer(), reader())


async def test_paste_content_readable_before_ingest_finishes(tmp_path: Path) -> None:
    """刚粘贴的文档要能立刻读回正文，不必等摄取跑完，**且内容与摄取完成后一致**。

    背景：解析缓存改为原子写之后，缓存文件在写完前根本不存在，
    ``GET /documents/{id}/content`` 会直接 404。对 paste/url 而言原文即 Markdown，
    没有理由让用户等向量化结束才能看到自己刚粘进去的内容。

    但回退读原文这条路一度**没有做解析路径同样的归一化**，于是同一个请求会因为
    后台摄取有没有跑完而返回不同的字节——这个测试随机挂 1/8，表现像时序抖动，
    实际是真的内容不一致。切片偏移是按解析后的正文算的，两者不一致会让引用高亮错位。
    所以这里断言的是**两条路径产出相同**，而不是等于原始输入
    （原始输入的首尾空白本就会被规范化掉）。
    """
    from fastapi.testclient import TestClient

    from agentmem.config import Settings
    from apps.api.main import create_app

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(data_dir=data_dir, models_config=tmp_path / "models.yaml")
    app = create_app(settings)
    api = "/api/v1"
    with TestClient(app) as client:
        space_id = client.post(f"{api}/spaces", json={"name": "t", "domain": "d"}).json()["id"]
        body = "# 标题\n\n    带缩进的正文\n"
        document = client.post(
            f"{api}/spaces/{space_id}/documents/paste", json={"title": "x", "content": body}
        ).json()
        url = f"{api}/spaces/{space_id}/documents/{document['id']}/content"
        response = client.get(url)
        assert response.status_code == 200
        early = response.json()["markdown"]

        # 正文本身必须完好：缩进不能被吃掉（ContentModel 守的就是这条）
        assert "    带缩进的正文" in early
        assert early.startswith("# 标题")

        # 等摄取跑完（缓存落地）后再读一次，两次必须**逐字节相同**
        for _ in range(100):
            if client.get(f"{api}/spaces/{space_id}/documents/{document['id']}").json()[
                "status"
            ] in ("ready", "failed"):
                break
            await asyncio.sleep(0.05)
        late = client.get(url).json()["markdown"]
        assert early == late, "摄取前后读到的正文不一致，切片偏移会对不上"


# -------------------------------------------------- ContentModel 完整性（Phase 2 审计补漏）


def test_citation_feed_preserves_streaming_whitespace() -> None:
    """流式解析器的实际输出路径必须保留空格。

    背景：Phase 2 首次修复只改了 ChatChunk，但真实流式路径经过
    CitationStreamParser → CitationFeed，后者当时仍继承 AgentMemModel（裁剪空格），
    导致 "Frida is a tool" 在解析后塌成 "Fridaisatool"。仅在 ChatChunk 层验证
    是不够的，必须验证端到端解析路径。
    """
    from agentmem.retrieve.citations import CitationStreamParser

    parser = CitationStreamParser()
    tokens = ["Frida", " ", "is", " ", "a", " ", "dynamic", " ", "tool"]
    assembled = "".join(parser.feed(token).text for token in tokens)
    assert assembled == "Frida is a dynamic tool"


def test_scored_chunk_preserves_code_indent() -> None:
    """检索命中的切片正文若是代码，缩进不能被裁剪。"""
    from agentmem.retrieve.models import ScoredChunk

    code = "    def hello():\n        return 42\n"
    chunk = ScoredChunk(chunk_id="c1", document_id="d1", content=code)
    assert chunk.content == code


def test_parse_result_preserves_offsets() -> None:
    """解析结果的 markdown 不能被裁剪，否则 char_start/char_end 全部错位。"""
    from agentmem.ingest.parse import ParseResult

    markdown = "   \n# 标题\n正文\n"
    result = ParseResult(markdown=markdown, parser="paste")
    assert result.markdown == markdown
    assert len(result.markdown) == len(markdown)


def test_citation_marker_prefix_is_normalized() -> None:
    """模型把 ``[^cN]`` 写成别的字母前缀时，归一到 ``cN`` 而不是丢掉。

    提示词规定 ``[^cN]``，但实测 agnes-2.5-flash 写成 ``[^e2]``（大概是 evidence）。
    此前 MARKER_PATTERN 只认 ``c``，这类标记既不解析也不从正文剥离：引用静默丢失，
    正文里还留下一段没人看得懂的 ``[^e2]``。一次实测统计里 11 个标记丢了 2 个。
    """
    from agentmem.retrieve.citations import CitationStreamParser

    parser = CitationStreamParser()
    feed = parser.feed("结论[^e2]，另见[^c1]。")

    assert feed.markers == ["c2", "c1"], "字母前缀应归一到 c，且保持出现顺序"
    assert "[^e2]" not in feed.text and "[^c1]" not in feed.text, "标记要从正文里剥掉"
    assert feed.text == "结论，另见。"


def test_plain_footnotes_are_not_swallowed_as_citations() -> None:
    """纯数字脚注与文字脚注不是引用标记，必须原样留在正文里。

    放宽前缀时容易误伤：``[^1]``（普通脚注）和 ``[^note]`` 都不该被当成引用吃掉。
    """
    from agentmem.retrieve.citations import CitationStreamParser

    parser = CitationStreamParser()
    feed = parser.feed("见脚注[^1]与[^note]。")
    rest = parser.flush()

    assert feed.markers == []
    assert "[^1]" in feed.text + rest
    assert "[^note]" in feed.text + rest


def test_stream_usage_is_read_from_frames_that_still_carry_choices() -> None:
    """usage 挂在「带 choices 的最后一帧」上时也要收下。

    OpenAI 兼容各家的 usage 帧形状不一：有的单独发一帧（``choices`` 为空），
    有的把 usage 挂在最后一帧上、``choices`` 里还留着一个空 delta——实测 agnes
    与部分聚合站属于后者。适配器此前只在 ``not event.choices`` 时才读 usage，
    后一种写法整帧被跳过，于是对话这一路的 ``prompt_tokens`` 永远是 None，
    设置页的「上下文成本与缓存命中」面板对主要工作负载永远没有数据。
    """
    import re
    from pathlib import Path

    src = Path("packages/core/agentmem/providers/adapters/openai_compatible.py").read_text()
    stream_body = src[src.index("        async for event in stream:") :]
    stream_body = stream_body[: stream_body.index("\n    async def ")]

    read_usage = stream_body.index('usage = getattr(event, "usage", None)')
    skip_empty = stream_body.index("if not event.choices:")
    assert read_usage < skip_empty, (
        "必须先无条件读 usage，再判断要不要跳过没有 choices 的帧；"
        "顺序反过来就会漏掉「usage 与 choices 同帧」的实现"
    )

    # 请求时要显式索取 usage，否则流式响应压根不带
    assert re.search(r"stream_options\s*=\s*\{\s*[\"']include_usage[\"']\s*:\s*True", src), (
        "流式请求要带 stream_options.include_usage"
    )


def test_context_overflow_is_recognized_from_provider_message() -> None:
    """上下文超限要被认成可恢复的 ContextOverflowError，而不是「服务不可用」。

    答案提示词带的是**全部**历史（为吃满前缀缓存，见 prompts/answer.py），
    会话够长必然撞窗口上限。认不出来就只能整轮硬失败，认出来才能裁短历史重试。
    各家措辞不统一，所以按关键词认。
    """
    import httpx
    from openai import APIStatusError

    from agentmem.errors import ContextOverflowError, ProviderUnavailableError
    from agentmem.providers.adapters.openai_compatible import translate_error

    def status_error(message: str) -> APIStatusError:
        request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
        response = httpx.Response(400, request=request, json={"error": {"message": message}})
        return APIStatusError(message, response=response, body=None)

    for wording in (
        "This model's maximum context length is 8192 tokens",
        "context_length_exceeded",
        "Please reduce the length of the messages",
    ):
        translated = translate_error(status_error(wording), "p1")
        assert isinstance(translated, ContextOverflowError), f"应认出上下文超限：{wording}"

    # 普通的 400 不能被误认
    other = translate_error(status_error("invalid api key"), "p1")
    assert isinstance(other, ProviderUnavailableError)
    assert not isinstance(other, ContextOverflowError)


@pytest.mark.parametrize("produced", [True, False])
async def test_chat_does_not_retry_after_output_or_without_history(
    produced: bool,
    database: Database,
    mock_registry: ProviderRegistry,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """以真实生成循环验证不能重放已输出内容，空历史也不能无限重试。"""
    from collections.abc import Sequence
    from time import perf_counter

    from agentmem.errors import ContextOverflowError
    from agentmem.retrieve.chat import ChatService, _GenerationState
    from agentmem.retrieve.citations import CitationRegistry
    from agentmem.retrieve.models import ChatEvent, DeltaChunk, QueryPlan, RetrievalResult
    from agentmem.types import ChatRequest

    calls = 0

    async def retrieve(
        self: ChatService, request: ChatRequest, history: Sequence[Message]
    ) -> RetrievalResult:
        return RetrievalResult(space_id="s", plan=QueryPlan(original=request.content))

    async def answer(
        self: ChatService,
        state: _GenerationState,
        messages: list[ProviderMessage],
        registry: CitationRegistry,
    ) -> AsyncIterator[ChatEvent]:
        nonlocal calls
        calls += 1
        if produced:
            yield ChatEvent(name="delta", payload=DeltaChunk(text="已输出的内容"))
        raise ContextOverflowError("上下文超限")

    monkeypatch.setattr(ChatService, "_retrieve", retrieve)
    monkeypatch.setattr(ChatService, "_generate_answer", answer)
    service = ChatService(
        space_id="s",
        database=database,
        registry=mock_registry,
        settings=settings,
    )
    state = _GenerationState(
        conversation_id="c",
        message_id="m",
        trace_id="t",
        query="q",
        llm_role="chat",
        started_at=perf_counter(),
        plan=QueryPlan(original="q"),
    )
    history = (
        [Message(id="u", conversation_id="c", role="user", content="历史问题", created_at=1)]
        if produced
        else []
    )
    events: list[ChatEvent] = []
    with pytest.raises(ContextOverflowError):
        async for event in service._generate(state, ChatRequest(content="q"), history):
            events.append(event)
    assert calls == 1
    assert sum(event.name == "delta" for event in events) == int(produced)


async def test_deleting_conversation_also_removes_its_traces_and_feedback(
    settings: Settings,
) -> None:
    """删会话要连轨迹与反馈一起删，不能只删消息。

    ``traces`` 对 ``messages`` / ``conversations`` 都没有外键，删会话时轨迹会原地留下。
    实测一个 Space 里 31 条轨迹有 23 条属于已删会话（72% 的轨迹正文体量），
    而轨迹里存着提问原文与检索到的内容——界面上的删除确认却写着「引用轨迹与反馈
    将一并移除」。更要紧的是 ``feedback`` 挂在轨迹上，不删轨迹，已删会话的反馈
    还会继续被蒸馏成经验。
    """
    from agentmem.space.manager import SpaceManager
    from agentmem.types import (
        ConversationCreate,
        FeedbackCreate,
        MessageCreate,
        SpaceCreate,
        TraceCreate,
    )

    manager = SpaceManager(settings)
    space = await manager.create_space(SpaceCreate(name="删除级联", domain="测试"))
    db = await manager.space_db(space.id)

    conv = await db.conversations.create(ConversationCreate(space_id=space.id, title="待删除"))
    msg = await db.messages.create(
        MessageCreate(conversation_id=conv.id, role="assistant", content="答案")
    )
    trace = await db.traces.create(
        TraceCreate(
            space_id=space.id,
            conversation_id=conv.id,
            message_id=msg.id,
            query="问题",
            retrieved=[],
        )
    )
    await db.feedback.create(FeedbackCreate(trace_id=trace.id, kind="up"))

    assert await db.traces.get(trace.id) is not None

    await db.conversations.delete(conv.id)

    assert await db.traces.get(trace.id) is None, "轨迹必须跟着会话一起删"
    remaining = await db.feedback.list_by_trace(trace.id)
    assert remaining == [], "反馈应随轨迹级联删除"


def test_all_ingest_paths_respect_the_reindex_lock() -> None:
    """三条摄取路径（上传/粘贴/URL）都要受重建索引锁保护。

    `add_url_document` 的 `runtime.require_unlocked(space_id)` 原本被误写在
    **docstring 里面**，函数也没收 `runtime` 参数——那两行永远不会执行。
    于是重建索引进行中（向量表正在被重建）仍能通过 URL 写入新文档。
    """
    import inspect

    from apps.api.routers import documents

    for handler in (
        documents.upload_documents,
        documents.add_paste_document,
        documents.add_url_document,
    ):
        source = inspect.getsource(handler)
        body = source[source.index('"""', source.index('"""') + 3) + 3 :]
        assert "runtime.require_unlocked(space_id)" in body, (
            f"{handler.__name__} 的锁检查必须在函数体里，不能在 docstring 中"
        )
        assert "runtime: RuntimeDep" in source, f"{handler.__name__} 得先拿到 runtime"


def test_upload_enforces_a_server_side_size_limit() -> None:
    """上传大小上限要在服务端也卡住。

    前端投喂弹窗写着「单文件 ≤ 100MB」，但后端此前完全不校验：界面上的限制形同虚设，
    直接调 API 时 `upload.read()` 会把整个文件读进内存。
    """
    from apps.api.routers.documents import MAX_UPLOAD_BYTES

    assert MAX_UPLOAD_BYTES == 100 * 1024 * 1024, "要与前端文案写的 100MB 一致"


async def test_oversized_upload_is_stopped_while_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    """超过上限的上传边读边停，不先整个读进内存；空文件同样拒绝。"""
    import io

    from fastapi import UploadFile

    from agentmem.errors import ValidationError
    from apps.api.routers import documents

    monkeypatch.setattr(documents, "MAX_UPLOAD_BYTES", 10)
    monkeypatch.setattr(documents, "UPLOAD_READ_CHUNK", 4)
    with pytest.raises(ValidationError, match="上限"):
        await documents._read_limited(UploadFile(io.BytesIO(b"x" * 11), filename="big.md"))
    with pytest.raises(ValidationError, match="为空"):
        await documents._read_limited(UploadFile(io.BytesIO(b""), filename="empty.md"))
    assert await documents._read_limited(UploadFile(io.BytesIO(b"x" * 10), filename="ok.md")) == (
        b"x" * 10
    )


async def test_export_refuses_while_ingestion_is_in_flight(settings: Settings) -> None:
    """摄取未完成时导出要拒绝，否则会产出缺向量的残包。

    向量是摄取末尾才写进 LanceDB 的，此刻打包得到的包里连 ``vectors/`` 都没有。
    而这种残包还原回来**不会报任何错**，检索只是静默退化成纯全文——实测复现过：
    还原后 ``legs`` 只剩 ``fts:1``，混合检索这条腿就这么没了。
    """
    from agentmem.errors import ValidationError
    from agentmem.ingest.bus import IngestBus
    from agentmem.space.manager import SpaceManager
    from agentmem.types import DocumentCreate, DocumentUpdate, SpaceCreate
    from apps.api.routers.spaces import export_space

    class _Runtime:
        def __init__(self, manager: SpaceManager) -> None:
            self.spaces = manager

        def require_unlocked(self, space_id: str) -> None:
            return None

        def maintain_space(self, space_id: str) -> AbstractContextManager[object]:
            return self.spaces.operation_gate(space_id).maintenance()

    bus = IngestBus()
    manager = SpaceManager(settings)
    space = await manager.create_space(SpaceCreate(name="导出守卫", domain="测试"))
    db = await manager.space_db(space.id)
    runtime = _Runtime(manager)

    # 还在摄取中：必须拒绝
    doc = await db.documents.create(
        DocumentCreate(space_id=space.id, title="半成品.md", source_type="paste", sha256="deadbeef")
    )
    await db.documents.update(doc.id, DocumentUpdate(status="embedding"))
    with pytest.raises(ValidationError):
        await export_space(runtime, bus, space.id)  # type: ignore[arg-type]

    # 摄取完成：放行
    await db.documents.update(doc.id, DocumentUpdate(status="ready"))
    response = await export_space(runtime, bus, space.id)  # type: ignore[arg-type]
    assert response.media_type == "application/zip"


async def test_importing_a_space_keeps_the_other_spaces_registered(
    settings: Settings,
) -> None:
    """导入一个 Space 不能把注册表里其他 Space 删掉。

    `_insert_space_row` 里有一句 ``DELETE FROM spaces WHERE id != ?``：对 Space
    **自己的 meta.db** 是对的（那个库里只该有自己这一行），但它同时也被用在
    **全局注册表**上——于是导入任何备份包，注册表里其他所有 Space 都会被删光。
    数据还在磁盘上，但界面上再也看不见；更糟的是 `import_zip` 里
    「注册表没有它、目录却还在 → 当作残骸 rmtree」的逻辑，会在下一次同 id 导入时
    把它真正删掉。实测复现过一次，真实 Space 就这么消失了。
    """
    from agentmem.space.manager import SpaceManager
    from agentmem.types import SpaceCreate

    manager = SpaceManager(settings)
    keeper = await manager.create_space(SpaceCreate(name="不该被动", domain="A"))
    victim = await manager.create_space(SpaceCreate(name="将被导出", domain="B"))

    payload = await manager.export_zip(victim.id)
    await manager.delete_space(victim.id)
    assert {s.id for s in await manager.list_spaces()} == {keeper.id}

    await manager.import_zip(payload)

    ids = {s.id for s in await manager.list_spaces()}
    assert keeper.id in ids, "导入不能把别的 Space 从注册表里挤掉"
    assert victim.id in ids, "导入的 Space 自己当然要在"


async def test_import_recycles_orphan_dir_instead_of_destroying_it(
    settings: Settings,
) -> None:
    """注册表没有、目录还在时，导入要把旧目录**移走**而不是 rmtree 掉。

    「注册表没有它 + 目录还在 = 早先删除留下的残骸」这个推断并不总成立：
    注册表自己也可能因为 bug 丢行（`DELETE FROM spaces WHERE id != ?` 就干过），
    那时目录里是用户仅存的数据，直接 rmtree 等于彻底销毁。
    """
    from agentmem.space.manager import SpaceManager
    from agentmem.types import SpaceCreate

    manager = SpaceManager(settings)
    space = await manager.create_space(SpaceCreate(name="回收验证", domain="测试"))
    payload = await manager.export_zip(space.id)

    # 只摘注册表行、保留磁盘目录，制造「注册表丢行」的局面
    await manager.delete_space(space.id, purge=False)
    space_dir = manager.space_dir(space.id)
    canary = space_dir / "canary.txt"
    canary.write_text("仅存数据")
    assert space_dir.exists()

    await manager.import_zip(payload)

    recycled_root = settings.data_dir / "recycled"
    survivors = list(recycled_root.glob(f"{space.id}-*/canary.txt"))
    assert survivors, "旧目录必须被移进回收目录，而不是被删掉"
    assert survivors[0].read_text() == "仅存数据"


async def test_orphan_space_dir_can_be_found_and_reregistered(settings: Settings) -> None:
    """注册表丢行后，磁盘上的 Space 要能被发现并重新登记。

    注册表少一行，这个 Space 就在界面与 API 里彻底消失，而数据完好躺在磁盘上。
    这种事真的发生过（导入备份时 `DELETE FROM spaces WHERE id != ?` 抹掉了其他
    所有 Space），当时产品内没有任何恢复办法，只能手写 SQL 补回去。
    """
    from agentmem.space.manager import SpaceManager
    from agentmem.types import SpaceCreate

    manager = SpaceManager(settings)
    space = await manager.create_space(SpaceCreate(name="孤儿", domain="测试"))

    # 只摘注册表行，保留磁盘数据——正是丢行事故的形状
    await manager.global_db.sqlite.execute("DELETE FROM spaces WHERE id = ?", (space.id,))
    assert space.id not in {s.id for s in await manager.list_spaces()}

    found = await manager.find_unregistered_space_dirs()
    assert (space.id, space.name) in found, "要能发现磁盘上这个没登记的 Space"

    recovered = await manager.reregister_space_dir(space.id)
    assert recovered.id == space.id
    assert space.id in {s.id for s in await manager.list_spaces()}, "登记回来后要能列出"


def test_models_yaml_writeback_keeps_a_self_describing_header(tmp_path: Path) -> None:
    """写回 models.yaml 要带上说明头，并且不落明文密钥。

    这个文件会被应用整体重写（「设置 → 模型」里改任何东西都触发），
    而 PyYAML 的 dumper 不保留注释——实测一次「启用某个 provider」抹掉了 125 行，
    包括文件头说明与各家云端 provider 的模板，而 README 正让用户去那里照抄。
    模板已挪去 `config/models.example.yaml`（应用从不写它），这里保证每次重写
    都会重新生成导航说明，用户不会面对一个没有任何线索的裸配置。
    """
    import os

    from agentmem.config import MODELS_YAML_HEADER, ModelsConfig, save_models_config
    from agentmem.types import ProviderConfig

    os.environ["_TEST_SECRET_KEY"] = "sk-should-never-be-written-to-disk"
    try:
        target = tmp_path / "models.yaml"
        config = ModelsConfig(
            providers=[
                ProviderConfig(
                    id="p1",
                    kind="llm",
                    adapter="openai_compatible",
                    model="m",
                    base_url="https://example.invalid/v1",
                    api_key="sk-should-never-be-written-to-disk",
                    api_key_ref="${_TEST_SECRET_KEY}",
                )
            ]
        )
        save_models_config(config, target)
        text = target.read_text(encoding="utf-8")

        assert text.startswith(MODELS_YAML_HEADER), "重写后必须带回说明头"
        assert "models.example.yaml" in text, "说明头要指向模板所在处"
        assert "sk-should-never-be-written-to-disk" not in text, "明文密钥不得落盘"
        assert "${_TEST_SECRET_KEY}" in text, "要还原成占位符"
    finally:
        os.environ.pop("_TEST_SECRET_KEY", None)


def test_eval_delta_threshold_scales_with_eval_set_size() -> None:
    """A/B 判定阈值必须随题量缩放，且按**干净**的噪声数据标定。

    原本写死 `MIN_EVAL_DELTA = 1.0`，几乎任何一次运行都会被判为「有显著变化」。
    第一次修正时拿到的噪声数据被「假零分」污染（逐题差值里出现 ±94、±97），
    阈值一度被定成 8 题 22.6 分——那会让进化闭环拒绝晋升真正有用的经验。
    修掉假零分后两次 A/A 实测逐题配对差值标准差 13.8。
    """
    from agentmem.expert.noise import EVAL_PAIRED_DIFF_SD, min_eval_delta

    # 题越少，要求的分差越大
    assert min_eval_delta(8) > min_eval_delta(30) > min_eval_delta(100)
    # 常量来自干净的实测（13.8），不能退回被污染的 32
    assert 10 <= EVAL_PAIRED_DIFF_SD <= 20, "噪声常量应来自修掉假零分后的实测"
    # 8 题约 10 分：高于 A/A 实测的总分差（-2.67、+6.0），又不至于什么都判不了
    assert 8 <= min_eval_delta(8) <= 12
    # 一题都没有：任何结论都不可信
    assert min_eval_delta(0) == 100.0


async def test_verdict_does_not_move_confidence_within_noise(settings: Settings) -> None:
    """配对差值不显著时，不许升也不许降。"""
    from agentmem.evolve.cycle import EvolutionService

    service = EvolutionService.__new__(EvolutionService)  # 只测纯判定逻辑
    # 8 题、均差 +5、波动 ±15：t 远小于临界值
    noisy = [20.0, -10.0, 15.0, -5.0, 10.0, 0.0, 5.0, 5.0]
    promoted, demoted = await EvolutionService._apply_verdict(service, [], 5.0, noisy)
    assert (promoted, demoted) == (0, 0)


def test_judge_missing_score_is_derived_from_dimensions_not_zero() -> None:
    """裁判漏写汇总 score 时按四维加权算回，绝不能默认成 0。

    此前 `float(data.get("score", 0.0))`：实测「四维全 1.0、verdict『全对』」
    的满分答案被记成零分，且 measured=True 计入均值。
    """
    from agentmem.expert.evaluator import EvaluationService

    raw = (
        '{"correctness": 1.0, "groundedness": 1.0, "completeness": 1.0, '
        '"standard_fit": 1.0, "verdict": "全对"}'
    )
    score, _reason, _audit = EvaluationService._parse(raw)
    assert score == 1.0


def test_judge_score_is_salvaged_from_malformed_json() -> None:
    """整段 JSON 因评语里的 ASCII 引号而非法时，分数要从原文抽回。

    实测失败样本里 score 都完整写在开头，坏的是后面 verdict 里的
    `不存在的"知识卡片"来源`。整条丢弃会让 8 题的有效样本掉到 6、7 题。
    真实的 0 分也要能抽回——真零分该算，假零分才不该算。
    """
    from agentmem.expert.evaluator import EvaluationService

    good = '{"score": 0.96, "verdict": "引用了不存在的"知识卡片"来源"}'
    assert EvaluationService._parse(good)[0] == 0.96

    real_zero = '{"score": 0.0, "verdict": "完全编造了"具体数值""}'
    assert EvaluationService._parse(real_zero)[0] == 0.0, "真实 0 分必须计入"

    assert EvaluationService._parse('{"verdict": "说不清"')[0] is None, "没分数就是未测得"
    assert EvaluationService._parse('{"score": 7.5, "x": "越界"y"')[0] is None, "越界不可信"


def test_paired_deltas_exclude_unmeasured_items() -> None:
    """逐题差值只在两臂都测得的题上算，不能拿占位 0 分去减。

    此前不看 measured：一边占位 0、一边测出 97，就凭空多出 +97。
    实测 A/A 逐题差值一度出现 ±94、±97，被误读成「裁判噪声约 ±32」，
    阈值常量据此被定得偏高约 2.3 倍。
    """
    from agentmem.expert.evaluator import _deltas
    from agentmem.types import EvalArmOutcome, EvalItemScore

    def arm(label: str, rows: list[tuple[str, float, bool]]) -> EvalArmOutcome:
        items = [
            EvalItemScore(item_id=i, score=sc, passed=sc >= 60, measured=m) for i, sc, m in rows
        ]
        measured = [x for x in items if x.measured]
        overall = sum(x.score for x in measured) / len(measured)
        return EvalArmOutcome(label=label, run_id="r", score=overall, item_scores=items)

    base = arm("A", [("q1", 80, True), ("q2", 0, False), ("q3", 70, True)])
    other = arm("B", [("q1", 84, True), ("q2", 97, True), ("q3", 66, True)])

    delta = _deltas([base, other])[0]
    ids = {d.item_id for d in delta.item_deltas}
    assert "q2" not in ids, "基准臂没测出 q2，不能拿占位 0 分去减出一个 +97"
    assert delta.score_delta == 0.0, "配对题 q1(+4) 与 q3(-4) 的均差应为 0"


def test_judge_weights_match_the_prompt_table() -> None:
    """解析端的权重必须与提示词里写给裁判的表格一致，防止两边漂移。"""
    import inspect

    from agentmem.prompts import judge
    from agentmem.prompts.judge import JUDGE_WEIGHTS

    source = inspect.getsource(judge)
    for key, weight in JUDGE_WEIGHTS.items():
        assert f"`{key}`" in source
        assert f"{weight:.2f}" in source, f"提示词表格里 {key} 的权重应为 {weight:.2f}"
    assert abs(sum(JUDGE_WEIGHTS.values()) - 1.0) < 1e-9


def test_more_evidence_candidates_never_render_fewer_items() -> None:
    """候选证据变多时，渲染出的条数不能反而变少，且不能超出证据区预算。

    证据区先按**全部**候选扣掉每条的标签开销，再分配正文预算；被预算丢掉的
    条目不会渲染，它们的开销却此前一直不还回来。实测：要 12 条证据只渲染 6 条，
    比要 8 条时的 7 条还少，2000 的预算里 361 token 白扣在不会出现的标签上。
    """
    import re

    from agentmem.prompts.answer import EVIDENCE_TOKEN_BUDGET, render_evidence
    from agentmem.prompts.budget import estimate_tokens

    row = (
        "| CMPD-{i:03d} | 奥希替尼类似物-{i:03d} | EGFR-T790M/C797S "
        "| {i}.10 nM | 12.5x | 临床前 | 无明显心脏毒性 |"
    )
    body = "\n".join(row.format(i=i) for i in range(6))  # 每条约两三百 token，与真实切片相当

    def chunks(count: int) -> list[dict[str, object]]:
        return [
            {
                "marker": f"c{i + 1}",
                "chunk_id": f"k{i}",
                "document_title": "EGFR评估.md",
                "heading_path": "化合物库 > 全量清单",
                "page": 1,
                "content": body,
                "score": 1.0,
            }
            for i in range(count)
        ]

    shown: list[int] = []
    for count in (5, 8, 12, 16):
        out = render_evidence(chunks(count))  # type: ignore[arg-type]
        assert estimate_tokens(out) <= EVIDENCE_TOKEN_BUDGET, f"{count} 条候选时超出了预算"
        shown.append(len(re.findall(r"<evidence marker=", out)))
    assert shown == sorted(shown), f"候选越多，渲染条数不应减少：{shown}"


async def test_eval_item_retries_transient_provider_errors() -> None:
    """限流等暂时性故障要退避重试，重试用尽才记为未测得。

    实测对比跑到一半 agnes 开始返回 429，降级链上其余 provider 又都不可用，
    一轮里 21 道题直接作废——等几十秒再试本可以测出来。
    """
    from agentmem.errors import ProviderUnavailableError
    from agentmem.expert.evaluator import EvaluationService
    from agentmem.types import EvalItem

    service = EvaluationService.__new__(EvaluationService)
    service.retry_delays = (0.0,)  # 测试里不真等
    item = EvalItem(
        id="q1", space_id="s", question="?", reference="r", must_include=[], created_at=0
    )
    calls = {"n": 0}

    async def flaky(*_args: object) -> EvalItemScore:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ProviderUnavailableError("agnes-flash 返回 HTTP 429")
        return EvalItemScore(item_id="q1", score=88.0, passed=True, measured=True)

    service._score_one_attempt = flaky  # type: ignore[method-assign,assignment]
    result = await service._score_one(item, [], None)  # type: ignore[arg-type]
    assert result.measured and result.score == 88.0, "第二次应当测出来"
    assert calls["n"] == 2

    # 一直失败：重试用尽后记为未测得，而不是 0 分
    async def always_down(*_args: object) -> EvalItemScore:
        raise ProviderUnavailableError("全部 provider 不可用")

    service._score_one_attempt = always_down  # type: ignore[method-assign,assignment]
    result = await service._score_one(item, [], None)  # type: ignore[arg-type]
    assert result.measured is False


def test_retrieval_reports_configured_but_unused_stages() -> None:
    """配置了却没用上的检索环节要报出来；没配置的不算降级。

    实测本机的 sentence-transformers 被误删后，本地 Embedding / Rerank 全部失败，
    检索静默退化成纯全文、没有重排——问答照常出结果，没有任何页面报错。
    """
    from agentmem.retrieve.pipeline import RetrievalPipeline, _EmbeddedQueries

    class _Registry:
        def __init__(self, roles: dict[str, str]) -> None:
            self.roles = roles

        def role_provider_id(self, role: str) -> str | None:
            return self.roles.get(role)

    def pipeline(roles: dict[str, str]) -> RetrievalPipeline:
        instance = RetrievalPipeline.__new__(RetrievalPipeline)
        instance.registry = _Registry(roles)  # type: ignore[assignment]
        return instance

    no_vectors = _EmbeddedQueries(primary=None)
    with_vectors = _EmbeddedQueries(primary=[0.1, 0.2])
    hits = [object()]  # 只看是否非空

    both = {"embedding": "local-embedding", "rerank": "local-reranker"}
    # 两个都配了、两个都没用上
    assert pipeline(both)._degradations("hybrid", no_vectors, hits, False) == [  # type: ignore[arg-type]
        "vector",
        "rerank",
    ]
    # 一切正常
    assert pipeline(both)._degradations("hybrid", with_vectors, hits, True) == []  # type: ignore[arg-type]
    # 压根没配 embedding / rerank：这是配置选择，不是故障
    assert pipeline({})._degradations("hybrid", no_vectors, hits, False) == []  # type: ignore[arg-type]
    # 纯全文模式本来就不用向量
    assert pipeline(both)._degradations("fts", no_vectors, hits, True) == []  # type: ignore[arg-type]
