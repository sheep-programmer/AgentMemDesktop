"""全量搜索必须覆盖首屏之外的资料，并保持隔离、字面关键词和分页计数。"""

from __future__ import annotations

from agentmem.store import Database
from agentmem.types import ConversationCreate, DocumentCreate, SpaceCreate


async def test_conversation_search_finds_old_records_and_paginates(database: Database) -> None:
    space = await database.spaces.create(SpaceCreate(name="搜索测试", domain="工程"))
    targets = []
    for index in range(4):
        conversation = await database.conversations.create(
            ConversationCreate(space_id=space.id, title=f"旧会话目标 {index}")
        )
        targets.append(conversation.id)
        await database.sqlite.execute(
            "UPDATE conversations SET updated_at = 0 WHERE id = ?", (conversation.id,)
        )
    for index in range(60):
        await database.conversations.create(
            ConversationCreate(space_id=space.id, title=f"最近讨论 {index}")
        )
    initial, total, _ = await database.conversations.list_by_space(space.id)
    assert len(initial) == 50
    assert total == 64
    assert not set(targets) & {item.id for item in initial}
    first, total, cursor = await database.conversations.list_by_space(
        space.id, query="目标", limit=2
    )
    assert total == 4
    assert cursor is not None
    second, total, cursor = await database.conversations.list_by_space(
        space.id, query="目标", limit=2, cursor=cursor
    )
    assert total == 4
    assert cursor is None
    assert {item.id for item in [*first, *second]} == set(targets)


async def test_conversation_search_is_literal_and_space_scoped(database: Database) -> None:
    space = await database.spaces.create(SpaceCreate(name="本空间", domain="工程"))
    other = await database.spaces.create(SpaceCreate(name="其他空间", domain="工程"))
    for title in ["100% 完成", "1000 完成", "snake_case", "snakeXcase", "C:\\docs"]:
        await database.conversations.create(ConversationCreate(space_id=space.id, title=title))
    await database.conversations.create(ConversationCreate(space_id=other.id, title="100% 完成"))
    for query, expected in [("100%", "100% 完成"), ("_", "snake_case"), ("\\", "C:\\docs")]:
        items, total, _ = await database.conversations.list_by_space(space.id, query=query)
        assert total == 1
        assert [item.title for item in items] == [expected]


async def test_document_search_is_literal_and_covers_sources(database: Database) -> None:
    space = await database.spaces.create(SpaceCreate(name="资料测试", domain="工程"))
    for index, (title, source) in enumerate(
        [("100% 指标", "report.md"), ("1000 指标", "other.md"), ("路径资料", "folder_name.md")]
    ):
        await database.documents.create(
            DocumentCreate(
                space_id=space.id,
                title=title,
                source_type="paste",
                source_uri=source,
                sha256=f"search-{index}",
            )
        )
    items, total, _ = await database.documents.list_by_space(space.id, query=" 100% ")
    assert total == 1
    assert items[0].title == "100% 指标"
    items, total, _ = await database.documents.list_by_space(space.id, query="_")
    assert total == 1
    assert items[0].title == "路径资料"
