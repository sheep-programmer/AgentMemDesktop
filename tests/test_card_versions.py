"""知识卡片的版本留档（G6）。

L2 卡片是「事实」，事实会变：新版指南把推荐剂量从 400mg 改成 200mg，旧值不该被
静默覆盖——「什么时候改的、原来是多少」在医药与合规场景里正是最该看得到的信息。
这里守两件事：**该留档的都留了**，以及**不该动的一分没动**（用户校订过的卡片、
没改变正文的合并都不产生新版本）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from agentmem.config import ModelsConfig
from agentmem.memory import CardService
from agentmem.providers.registry import ProviderRegistry
from agentmem.store import Database
from agentmem.types import (
    KnowledgeCardCreate,
    KnowledgeCardRequest,
    KnowledgeCardUpdate,
    SpaceCreate,
)


def _registry() -> ProviderRegistry:
    """不绑定任何角色的注册表：本文件的用例都不需要模型。"""
    return ProviderRegistry(ModelsConfig(providers=[], roles={}))


@pytest.fixture
async def space(tmp_path: Path) -> AsyncIterator[tuple[Database, str, CardService]]:
    database = Database(tmp_path / "meta.db", tmp_path / "vectors", "space-x")
    await database.open()
    created = await database.spaces.create(SpaceCreate(name="新药研发", domain="药物发现"))
    service = CardService(space_id=created.id, database=database, registry=_registry())
    try:
        yield database, created.id, service
    finally:
        await database.close()


async def test_manual_edit_keeps_the_previous_body(
    space: tuple[Database, str, CardService],
) -> None:
    """人工改正文：旧内容必须留在版本历史里，不是被覆盖掉。"""
    _database, space_id, service = space
    card = await service.create(
        KnowledgeCardRequest(kind="fact", title="XX 的推荐剂量", body="推荐剂量 400mg")
    )

    await service.update(card.id, KnowledgeCardUpdate(body="推荐剂量 200mg"))

    history = await service.history(card.id)
    assert history.card.body == "推荐剂量 200mg"
    assert len(history.versions) == 1
    previous = history.versions[0]
    assert previous.version == 1
    assert previous.body == "推荐剂量 400mg"
    assert previous.title == "XX 的推荐剂量"
    assert previous.valid_to >= previous.valid_from
    assert previous.space_id == space_id


async def test_repeated_edits_stack_versions(space: tuple[Database, str, CardService]) -> None:
    """改两次就有两版历史，新的在前，版本号递增。"""
    _database, _space_id, service = space
    card = await service.create(KnowledgeCardRequest(kind="fact", title="清除率", body="第 1 版"))
    await service.update(card.id, KnowledgeCardUpdate(body="第 2 版"))
    await service.update(card.id, KnowledgeCardUpdate(body="第 3 版"))

    history = await service.history(card.id)

    assert history.card.body == "第 3 版"
    assert [(item.version, item.body) for item in history.versions] == [
        (2, "第 2 版"),
        (1, "第 1 版"),
    ]


async def test_editing_only_aliases_does_not_create_a_version(
    space: tuple[Database, str, CardService],
) -> None:
    """只改别名不是「事实变了」，不该产生版本噪音。"""
    _database, _space_id, service = space
    card = await service.create(
        KnowledgeCardRequest(kind="concept", title="TDI", body="时间依赖性抑制")
    )

    await service.update(card.id, KnowledgeCardUpdate(aliases=["time-dependent inhibition"]))

    history = await service.history(card.id)
    assert history.versions == []


async def test_extraction_replacing_a_body_archives_the_old_one(
    space: tuple[Database, str, CardService],
) -> None:
    """自动抽取用更高置信度的正文覆盖时，同样留档。"""
    _database, space_id, service = space
    await service.merge_extracted(
        KnowledgeCardCreate(
            space_id=space_id, kind="fact", title="溶解度阈值", body="旧值 50 µM", confidence=0.4
        )
    )

    replaced, _created = await service.merge_extracted(
        KnowledgeCardCreate(
            space_id=space_id, kind="fact", title="溶解度阈值", body="新值 10 µM", confidence=0.8
        )
    )
    history = await service.history(replaced.id)

    assert history.card.body == "新值 10 µM"
    assert [item.body for item in history.versions] == ["旧值 50 µM"]
    # 合并结果是替换而不是新建：来源切片取并集、卡片只有一张
    assert history.versions[0].version == 1
    assert _created is False, "同名同类是合并而不是新建"


async def test_extraction_without_body_change_does_not_archive(
    space: tuple[Database, str, CardService],
) -> None:
    """正文没变只是置信度更高：不算新版本。"""
    _database, space_id, service = space
    await service.merge_extracted(
        KnowledgeCardCreate(
            space_id=space_id, kind="fact", title="hERG", body="阻断会延长 QT", confidence=0.3
        )
    )
    merged, _created = await service.merge_extracted(
        KnowledgeCardCreate(
            space_id=space_id, kind="fact", title="hERG", body="阻断会延长 QT", confidence=0.9
        )
    )

    history = await service.history(merged.id)

    assert history.versions == []
    assert _created is False


async def test_user_verified_card_is_never_overwritten(
    space: tuple[Database, str, CardService],
) -> None:
    """用户校订过的卡片连正文都不该被自动抽取改，自然也没有历史版本。"""
    _database, space_id, service = space
    card = await service.create(
        KnowledgeCardRequest(kind="fact", title="剂量", body="用户写下的 200mg")
    )

    merged, _created = await service.merge_extracted(
        KnowledgeCardCreate(
            space_id=space_id, kind="fact", title="剂量", body="模型写下的 400mg", confidence=1.0
        )
    )

    assert merged.body == "用户写下的 200mg"
    assert (await service.history(card.id)).versions == []


async def test_history_survives_reload_and_deletion_cascades(
    space: tuple[Database, str, CardService],
) -> None:
    """换个句柄读到的历史一致；删卡片时历史随外键一起清掉。"""
    database, _space_id, service = space
    card = await service.create(KnowledgeCardRequest(kind="fact", title="半衰期", body="旧值 4h"))
    await service.update(card.id, KnowledgeCardUpdate(body="新值 6h"))

    fresh = CardService(space_id=card.space_id, database=database, registry=_registry())
    assert [item.body for item in (await fresh.history(card.id)).versions] == ["旧值 4h"]

    await service.delete(card.id)
    assert await database.card_versions.list_by_card(card.id) == []


async def test_saving_unchanged_text_does_not_create_a_version(
    space: tuple[Database, str, CardService],
) -> None:
    """标题正文原样提交（前端每次保存都带上它们）不该留一个逐字相同的历史版本。"""
    _database, _space_id, service = space
    card = await service.create(
        KnowledgeCardRequest(kind="concept", title="TDI", body="时间依赖性抑制")
    )
    await service.update(
        card.id, KnowledgeCardUpdate(title=card.title, body=card.body, kind="fact")
    )
    history = await service.history(card.id)
    assert history.versions == []
