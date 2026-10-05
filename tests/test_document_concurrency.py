"""跨请求摄取、维护与删除必须共用生命周期保护。"""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import UploadFile

from agentmem.config import Settings
from agentmem.errors import DuplicateDocumentError, SpaceLockedError
from agentmem.ingest.bus import IngestBus
from agentmem.ingest.pipeline import IngestPipeline, ProgressCallback, StatusCallback
from agentmem.space.runtime import Runtime
from agentmem.types import Document, DocumentCreate, SpaceCreate, UploadResponse
from apps.api.routers.documents import _start_ingest, upload_documents


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[tuple[Runtime, str, Document]]:
    models = tmp_path / "models.yaml"
    models.write_text("providers: []\nroles: {}", encoding="utf-8")
    runtime = Runtime(
        Settings(data_dir=tmp_path / "data", models_config=models, warmup_on_start=False)
    )
    await runtime.start()
    space = await runtime.spaces.create_space(SpaceCreate(name="并发测试", domain="工程"))
    database = await runtime.spaces.space_db(space.id)
    document = await database.documents.create(
        DocumentCreate(space_id=space.id, title="资料", source_type="paste", sha256="test")
    )
    try:
        yield runtime, space.id, document
    finally:
        await runtime.stop()


async def test_repeated_maintenance_lock_is_rejected(world: tuple[Runtime, str, Document]) -> None:
    runtime, space_id, _document = world
    runtime.lock_space(space_id)
    try:
        with pytest.raises(SpaceLockedError):
            runtime.lock_space(space_id)
        assert runtime.is_space_locked(space_id)
    finally:
        runtime.unlock_space(space_id)


async def test_delete_cannot_modify_a_space_under_maintenance(
    world: tuple[Runtime, str, Document],
) -> None:
    runtime, space_id, document = world
    runtime.lock_space(space_id)
    try:
        with pytest.raises(SpaceLockedError):
            await runtime.delete_document(space_id, document.id)
        database = await runtime.spaces.space_db(space_id)
        assert await database.documents.get(document.id) is not None
    finally:
        runtime.unlock_space(space_id)


async def test_pipeline_instances_serialize_the_same_document(
    world: tuple[Runtime, str, Document], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, space_id, document = world
    database = await runtime.spaces.space_db(space_id)
    first = IngestPipeline(database, runtime.registry(), runtime.settings)
    second = IngestPipeline(database, runtime.registry(), runtime.settings)
    entered = asyncio.Event()
    release = asyncio.Event()
    started = 0

    async def parsing(
        self: IngestPipeline,
        doc: Document,
        progress: ProgressCallback | None,
        status: StatusCallback | None,
    ) -> None:
        nonlocal started
        started += 1
        entered.set()
        await release.wait()

    monkeypatch.setattr(IngestPipeline, "_stage_parsing", parsing)
    a = asyncio.create_task(first.run(document.id, stages=["parsing"]))
    await entered.wait()
    b = asyncio.create_task(second.run(document.id, stages=["parsing"]))
    try:
        await asyncio.sleep(0.01)
        assert started == 1, "第二个请求不能绕过第一个请求的文档锁"
    finally:
        release.set()
        await asyncio.gather(a, b)
    assert started == 2


async def test_pending_ingest_cannot_be_queued_twice(
    world: tuple[Runtime, str, Document], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, space_id, document = world
    database = await runtime.spaces.space_db(space_id)
    pipeline = IngestPipeline(database, runtime.registry(), runtime.settings)
    bus = IngestBus(concurrency=1)

    async def run(self: IngestPipeline, document_id: str, **kwargs: object) -> Document:
        await asyncio.Event().wait()
        return document

    monkeypatch.setattr(IngestPipeline, "run", run)
    try:
        _start_ingest(bus, pipeline, document, space_id)
        with pytest.raises(SpaceLockedError):
            _start_ingest(bus, pipeline, document, space_id)
        assert bus.running_tasks(space_id) == 1
    finally:
        await bus.shutdown()


async def test_cancelled_ingest_is_immediately_retryable(
    world: tuple[Runtime, str, Document], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, space_id, document = world
    database = await runtime.spaces.space_db(space_id)
    pipeline = IngestPipeline(database, runtime.registry(), runtime.settings)
    entered = asyncio.Event()

    async def parsing(
        self: IngestPipeline,
        doc: Document,
        progress: ProgressCallback | None,
        status: StatusCallback | None,
    ) -> None:
        await self.db.documents.set_status(doc.id, "parsing")
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(IngestPipeline, "_stage_parsing", parsing)
    task = asyncio.create_task(pipeline.run(document.id, stages=["parsing"]))
    await entered.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    updated = await database.documents.require(document.id)
    assert updated.status == "failed"
    assert updated.error
    with runtime.maintain_space(space_id):
        pass


async def test_reserved_job_blocks_maintenance_and_deletion(
    world: tuple[Runtime, str, Document], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, space_id, document = world
    database = await runtime.spaces.space_db(space_id)
    pipeline = IngestPipeline(database, runtime.registry(), runtime.settings)
    bus = IngestBus()

    async def run(self: IngestPipeline, document_id: str, **kwargs: object) -> Document:
        await asyncio.Event().wait()
        return document

    monkeypatch.setattr(IngestPipeline, "run", run)
    try:
        _start_ingest(bus, pipeline, document, space_id)
        with pytest.raises(SpaceLockedError):
            runtime.lock_space(space_id)
        with pytest.raises(SpaceLockedError):
            await runtime.delete_document(space_id, document.id)
    finally:
        await bus.shutdown()
    with runtime.maintain_space(space_id):
        pass


async def test_maintenance_permit_allows_its_own_pipeline_only(
    world: tuple[Runtime, str, Document],
) -> None:
    runtime, space_id, document = world
    database = await runtime.spaces.space_db(space_id)
    with runtime.maintain_space(space_id) as token:
        outsider = IngestPipeline(database, runtime.registry(), runtime.settings)
        owner = IngestPipeline(
            database, runtime.registry(), runtime.settings, operation_token=token
        )
        with pytest.raises(SpaceLockedError):
            await outsider.run(document.id, stages=["unsupported-noop"])
        assert (await owner.run(document.id, stages=["unsupported-noop"])).status == "ready"


async def test_parallel_duplicate_registration_reports_a_conflict(
    world: tuple[Runtime, str, Document],
) -> None:
    runtime, space_id, _document = world
    database = await runtime.spaces.space_db(space_id)
    data = DocumentCreate(
        space_id=space_id, title="重复资料", source_type="paste", sha256="duplicate"
    )
    results = await asyncio.gather(
        database.documents.create(data), database.documents.create(data), return_exceptions=True
    )
    assert sum(isinstance(result, Document) for result in results) == 1
    assert sum(isinstance(result, DuplicateDocumentError) for result in results) == 1


async def test_duplicate_upload_cleanup_preserves_the_winners_raw_file(
    world: tuple[Runtime, str, Document], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, space_id, document = world
    database = await runtime.spaces.space_db(space_id)
    pipeline = IngestPipeline(database, runtime.registry(), runtime.settings)
    bus = IngestBus()
    payload = b"# Shared upload\n\nConcurrent duplicate content."

    async def run(self: IngestPipeline, document_id: str, **kwargs: object) -> Document:
        await asyncio.Event().wait()
        return document

    monkeypatch.setattr(IngestPipeline, "run", run)
    files = [UploadFile(io.BytesIO(payload), filename="shared.md") for _ in range(2)]
    try:
        results = await asyncio.gather(
            *(upload_documents(space_id, runtime, bus, pipeline, [file]) for file in files),
            return_exceptions=True,
        )
        successes = [item for item in results if isinstance(item, UploadResponse)]
        assert len(successes) == 1
        assert sum(isinstance(item, DuplicateDocumentError) for item in results) == 1
        winner = successes[0].documents[0]
        assert winner.source_uri is not None
        assert await asyncio.to_thread(Path(winner.source_uri).read_bytes) == payload
        assert len(list(runtime.spaces.raw_dir(space_id).iterdir())) == 1
    finally:
        await bus.shutdown()
        for file in files:
            await file.close()
