"""Space 生命周期：导出、导入、删除。

导入是「换台机器接着用」唯一的路径，也是最容易被写坏的一条：它要跨进程搬运
一个 SQLite 库、一个向量库和一堆原始文件，任何一步顺序错了都可能是静默的数据丢失
而不是报错。这里把每一步都钉住。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agentmem.config import get_settings
from agentmem.space.manager import SpaceManager
from agentmem.types import DocumentCreate, SpaceCreate


def _document_count(path: Path) -> int:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
    finally:
        connection.close()


@pytest.fixture
async def manager(tmp_path: Path) -> SpaceManager:
    instance = SpaceManager(get_settings().model_copy(update={"data_dir": tmp_path / "data"}))
    await instance.open()
    return instance


async def _space_with_document(manager: SpaceManager, title: str = "笔记") -> tuple[str, str]:
    space = await manager.create_space(SpaceCreate(name="新药研发", domain="药物发现"))
    database = await manager.space_db(space.id)
    document = await database.documents.create(
        DocumentCreate(
            space_id=space.id,
            title=title,
            source_type="paste",
            mime="text/markdown",
            sha256="a" * 64,
            size_bytes=3,
        )
    )
    return space.id, document.id


async def test_import_restores_documents(manager: SpaceManager) -> None:
    """导入回来的 Space 必须带着它的文档。

    这里曾经踩过一个静默的坑：把 Space 行写回自身 meta.db 时用了
    ``INSERT OR REPLACE``，它的语义是「先删同主键行再插入」，而 ``documents``
    等表都是 ``REFERENCES spaces(id) ON DELETE CASCADE``——于是「刷新 space 行」
    顺着外键把导入进来的整库数据删干净了。导入返回成功，Space 却是空的。
    """
    space_id, document_id = await _space_with_document(manager)
    payload = await manager.export_zip(space_id)

    await manager.delete_space(space_id)
    assert not manager.space_dir(space_id).exists()

    imported = await manager.import_zip(payload)

    assert imported.id == space_id
    assert _document_count(manager.meta_db_path(space_id)) == 1
    database = await manager.space_db(space_id)
    assert (await database.documents.require(document_id)).title == "笔记"


async def test_import_twice_is_rejected(manager: SpaceManager) -> None:
    """同一个 Space 已存在时拒绝导入，不覆盖既有数据。"""
    space_id, _ = await _space_with_document(manager)
    payload = await manager.export_zip(space_id)

    with pytest.raises(Exception) as excinfo:
        await manager.import_zip(payload)

    assert "已存在" in str(excinfo.value)
    assert _document_count(manager.meta_db_path(space_id)) == 1


async def test_delete_space_removes_data_by_default(manager: SpaceManager) -> None:
    """删除 Space 默认连磁盘数据一起删。

    留着不删的话，界面上看不见、磁盘上却还躺着全部资料，同 id 也再也导不回来。
    """
    space_id, _ = await _space_with_document(manager)
    directory = manager.space_dir(space_id)
    assert directory.exists()

    await manager.delete_space(space_id)

    assert not directory.exists()

    # 显式要求保留时才留下目录
    kept_id, _ = await _space_with_document(manager, title="保留")
    kept_directory = manager.space_dir(kept_id)
    await manager.delete_space(kept_id, purge=False)
    assert kept_directory.exists()


async def test_deleting_without_purge_does_not_block_reimport(manager: SpaceManager) -> None:
    """只摘注册表留下的残骸不该挡住同 id 的再次导入。"""
    space_id, _ = await _space_with_document(manager)
    payload = await manager.export_zip(space_id)

    await manager.delete_space(space_id, purge=False)
    assert manager.space_dir(space_id).exists()

    imported = await manager.import_zip(payload)

    assert imported.id == space_id
    assert _document_count(manager.meta_db_path(space_id)) == 1


async def test_missing_space_yaml_is_rebuilt_from_the_space_record(tmp_path: Path) -> None:
    """space.yaml 缺失时用 Space 记录重建，而不是让领域变成空字符串。

    空领域会让所有提示词变成「你是「」领域的资深专家」——领域大纲、卡片抽取、经验
    蒸馏全都跑不出东西，而界面上看不出任何异常。实测就是靠这条发现一个 Space 的
    persona 领域是空的：模型返回了空大纲。
    """
    from agentmem.config import get_settings
    from agentmem.space import SpaceManager

    settings = get_settings().model_copy(update={"data_dir": tmp_path / "data"})
    manager = SpaceManager(settings)
    space = await manager.create_space(SpaceCreate(name="新药研发", domain="药物发现"))

    # 模拟历史遗留：配置文件丢失
    manager.yaml_path(space.id).unlink()

    config = await manager.read_config(space.id)

    assert config.persona.domain == "药物发现"
    assert "药物发现" in config.persona.role_description
    assert manager.yaml_path(space.id).is_file(), "重建后要落盘，不能每次都重算"


async def test_empty_persona_domain_is_repaired(tmp_path: Path) -> None:
    """已有配置文件但领域为空时同样修复。"""
    from agentmem.config import get_settings
    from agentmem.space import SpaceManager

    settings = get_settings().model_copy(update={"data_dir": tmp_path / "data"})
    manager = SpaceManager(settings)
    space = await manager.create_space(SpaceCreate(name="逆向", domain="Android 逆向"))
    config = await manager.read_config(space.id)
    await manager.write_config(
        space.id,
        config.model_copy(update={"persona": config.persona.model_copy(update={"domain": ""})}),
    )

    repaired = await manager.read_config(space.id)

    assert repaired.persona.domain == "Android 逆向"
