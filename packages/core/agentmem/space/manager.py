"""Space 生命周期管理：CRUD、目录初始化、space.yaml 读写、导入导出。

磁盘布局（``docs/01-ARCHITECTURE.md`` §2）::

    data/spaces/<space_id>/
      meta.db       # 该 Space 的 SQLite（含自己的 spaces 行，满足外键约束）
      vectors/      # LanceDB 向量表
      raw/          # 原始文件
      space.yaml    # Persona / 模型覆盖 / 检索参数

全局库 ``data/agentmem.db`` 保存 Space 注册表与跨 Space 记录（用量、global 经验）。
"""

from __future__ import annotations

import asyncio
import re
import shutil
import time
from pathlib import Path

import aiofiles
import structlog

from agentmem.config import Settings, get_settings, load_space_yaml, save_space_yaml
from agentmem.errors import NotFoundError, ValidationError
from agentmem.security import ensure_within
from agentmem.space.archive import (
    META_DB_NAME,
    RAW_DIR_NAME,
    SPACE_YAML_NAME,
    VECTORS_DIR_NAME,
    export_space_zip,
    import_space_zip,
)
from agentmem.store import Database
from agentmem.store.operations import OperationGate
from agentmem.types import (
    Persona,
    Space,
    SpaceCreate,
    SpaceUpdate,
    SpaceYaml,
)

logger = structlog.get_logger(__name__)

GLOBAL_DB_NAME = "agentmem.db"


class SpaceManager:
    """多知识库隔离与生命周期。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._global: Database | None = None
        self._space_dbs: dict[str, Database] = {}
        self._operation_gates: dict[str, OperationGate] = {}
        self._lock = asyncio.Lock()

    # -- 生命周期 ---------------------------------------------------------

    async def open(self) -> None:
        """初始化目录并打开全局库。"""
        self.settings.ensure_dirs()
        if self._global is None:
            database = Database(self.global_db_path)
            await database.open()
            self._global = database

    async def close(self) -> None:
        """关闭全部数据库。"""
        async with self._lock:
            for database in self._space_dbs.values():
                await database.close()
            self._space_dbs.clear()
        if self._global is not None:
            await self._global.close()
            self._global = None

    @property
    def global_db(self) -> Database:
        """全局库。"""
        if self._global is None:
            raise RuntimeError("SpaceManager 尚未 open()")
        return self._global

    # -- 路径 -------------------------------------------------------------

    @property
    def spaces_root(self) -> Path:
        """全部 Space 的根目录。"""
        return self.settings.spaces_dir

    @property
    def global_db_path(self) -> Path:
        """全局库文件路径。"""
        return self.settings.data_dir / GLOBAL_DB_NAME

    def space_dir(self, space_id: str) -> Path:
        """某 Space 的目录。

        ``space_id`` 未必可信——它可能来自导入压缩包里的 ``meta.db``，
        而那个文件的外部来源完全不受控。所以这里必须做一次沙箱检查：
        凡是拼出 ``spaces/../../x`` 这种路径的 id 一律拒绝，不允许落到磁盘上。
        """
        return ensure_within(self.spaces_root, self.spaces_root / space_id)

    def meta_db_path(self, space_id: str) -> Path:
        """某 Space 的 SQLite 文件。"""
        return self.space_dir(space_id) / META_DB_NAME

    def vectors_dir(self, space_id: str) -> Path:
        """某 Space 的向量库目录。"""
        return self.space_dir(space_id) / VECTORS_DIR_NAME

    def raw_dir(self, space_id: str) -> Path:
        """某 Space 的原始文件目录。"""
        return self.space_dir(space_id) / RAW_DIR_NAME

    def yaml_path(self, space_id: str) -> Path:
        """某 Space 的 space.yaml。"""
        return self.space_dir(space_id) / SPACE_YAML_NAME

    def init_space_dir(self, space_id: str) -> Path:
        """创建 Space 目录骨架。"""
        directory = self.space_dir(space_id)
        for child in (directory, self.raw_dir(space_id), self.vectors_dir(space_id)):
            child.mkdir(parents=True, exist_ok=True)
        return directory

    # -- 每 Space 的数据库 -------------------------------------------------

    def operation_gate(self, space_id: str) -> OperationGate:
        """数据库重新打开后仍沿用同一空间操作保护。"""
        gate = self._operation_gates.get(space_id)
        if gate is None:
            gate = OperationGate()
            self._operation_gates[space_id] = gate
        return gate

    async def space_db(self, space_id: str) -> Database:
        """取（并缓存）某 Space 的数据库句柄。"""
        await self.open()
        async with self._lock:
            database = self._space_dbs.get(space_id)
            if database is None:
                self.init_space_dir(space_id)
                database = Database(
                    self.meta_db_path(space_id),
                    self.vectors_dir(space_id),
                    space_id,
                    operations=self.operation_gate(space_id),
                )
                await database.open()
                self._space_dbs[space_id] = database
            return database

    async def close_space(self, space_id: str) -> None:
        """关闭并移除某 Space 的数据库缓存。"""
        async with self._lock:
            database = self._space_dbs.pop(space_id, None)
        if database is not None:
            await database.close()

    # -- CRUD -------------------------------------------------------------

    async def list_spaces(self) -> list[Space]:
        """列出全部 Space。"""
        await self.open()
        return await self.global_db.spaces.list_all()

    async def get_space(self, space_id: str) -> Space | None:
        """按 id 取 Space。"""
        await self.open()
        return await self.global_db.spaces.get(space_id)

    async def require_space(self, space_id: str) -> Space:
        """按 id 取 Space，不存在则抛 ``NOT_FOUND``。"""
        await self.open()
        return await self.global_db.spaces.require(space_id)

    async def create_space(self, data: SpaceCreate) -> Space:
        """新建 Space：写注册表、建目录、落初始 Persona。"""
        await self.open()
        space = await self.global_db.spaces.create(data)
        self.init_space_dir(space.id)
        await self.write_config(space.id, SpaceYaml(persona=self.default_persona(space)))
        await self._mirror_space(space)
        logger.info("space_created", space_id=space.id, name=space.name)
        return space

    async def find_unregistered_space_dirs(self) -> list[tuple[str, str]]:
        """找出磁盘上有目录、注册表里却没有的 Space，返回 ``(space_id, name)``。

        注册表丢一行，这个 Space 就在界面与 API 里彻底消失，而数据完好躺在磁盘上，
        产品内没有任何办法把它找回来——实测发生过一次（`_insert_space_row` 的
        `DELETE FROM spaces WHERE id != ?` 在导入时抹掉了其他所有 Space），
        当时只能手写 SQL 补回注册表行。

        每个 Space 自己的 ``meta.db`` 里都镜像着一份自己的 spaces 行（见
        `_mirror_space`），所以恢复所需的信息本来就在磁盘上。这里只负责**发现**，
        要不要重新注册由调用方决定：``delete_space(purge=False)`` 是显式保留数据的
        删除，不该被自动复活。
        """
        await self.open()
        registered = {space.id for space in await self.list_spaces()}
        found: list[tuple[str, str]] = []
        if not self.spaces_root.exists():
            return found
        for directory in sorted(self.spaces_root.iterdir()):
            # `.import-*` 是导入用的临时目录，不是 Space
            if not directory.is_dir() or directory.name.startswith("."):
                continue
            if directory.name in registered:
                continue
            meta = directory / META_DB_NAME
            if not meta.is_file():
                continue
            database = Database(meta, directory / VECTORS_DIR_NAME)
            try:
                await database.open()
                rows = await database.sqlite.fetchall("SELECT * FROM spaces LIMIT 1")
                if rows:
                    space = Space.model_validate(dict(rows[0]))
                    found.append((space.id, space.name))
            except Exception:  # pragma: no cover - 损坏的目录跳过即可
                logger.warning("unregistered_space_unreadable", path=str(directory))
            finally:
                await database.close()
        return found

    async def reregister_space_dir(self, space_id: str) -> Space:
        """把磁盘上的孤儿 Space 目录重新登记进注册表。"""
        await self.open()
        if await self.global_db.spaces.get(space_id) is not None:
            raise ValidationError("该 Space 已在注册表中", detail={"space_id": space_id})
        directory = self.space_dir(space_id)
        meta = directory / META_DB_NAME
        if not meta.is_file():
            raise NotFoundError("Space 目录", space_id)
        database = Database(meta, directory / VECTORS_DIR_NAME)
        await database.open()
        try:
            rows = await database.sqlite.fetchall("SELECT * FROM spaces LIMIT 1")
            if not rows:
                raise ValidationError("该目录的 meta.db 缺少 spaces 记录")
            space = Space.model_validate(dict(rows[0]))
        finally:
            await database.close()
        await self._insert_space_row(self.global_db, space)
        logger.info("space_reregistered", space_id=space.id, name=space.name)
        return space

    def _recycle_dir(self, space_id: str) -> Path:
        """给「不敢直接删」的 Space 目录找一个带时间戳的回收位置。

        放在 ``data/recycled/`` 下而不是 ``data/spaces/`` 里：后者会被 Space 扫描
        逻辑看见，留在那儿等于凭空多出一个半残的 Space。
        """
        stamp = time.strftime("%Y%m%d-%H%M%S")
        return self.settings.data_dir / "recycled" / f"{space_id}-{stamp}"

    async def _mirror_space(self, space: Space) -> None:
        """把 Space 行写入其自身 meta.db（满足该库内的外键约束）。"""
        database = await self.space_db(space.id)
        # Space 自己的库里只该有自己这一行
        await self._insert_space_row(database, space, sole_row=True)

    @staticmethod
    async def _insert_space_row(
        database: Database, space: Space, *, sole_row: bool = False
    ) -> None:
        """把 Space 行写进某个库（全局注册表，或 Space 自己的 meta.db）。

        ``sole_row=True`` 表示这个库里只该有这一行——只有 Space **自己的 meta.db**
        满足这个前提。全局注册表必须留着所有 Space，所以它一定要用默认的 False。
        这个开关原本不存在，两种库共用同一段逻辑并无条件执行
        ``DELETE FROM spaces WHERE id != ?``：导入任何一个备份包，
        注册表里**其他所有 Space 都会被删掉**。数据还躺在磁盘上，但界面上再也看不见，
        而 `import_zip` 里「注册表没有它、目录却还在 → 当作残骸 rmtree」的逻辑
        会在下一次同 id 导入时把它真正删掉。实测复现过，主 Space 就这么消失过一次。

        ⚠️ 这里**不能**用 ``INSERT OR REPLACE``：它的语义是「先删同主键的行再插入」，
        而 ``documents`` / ``chunks`` 等表都是 ``REFERENCES spaces(id) ON DELETE CASCADE``，
        删除会顺着外键把整个 Space 的数据一起带走。导入 Space 时那行本来就在库里，
        于是「刷新一下 space 行的名字」变成了「清空这个 Space」。改用 UPSERT：
        冲突时只更新字段，不产生删除。
        """
        if sole_row:
            await database.sqlite.execute("DELETE FROM spaces WHERE id != ?", (space.id,))
        await database.sqlite.execute(
            "INSERT INTO spaces (id, name, domain, icon, color, description,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET"
            " name = excluded.name, domain = excluded.domain, icon = excluded.icon,"
            " color = excluded.color, description = excluded.description,"
            " updated_at = excluded.updated_at",
            (
                space.id,
                space.name,
                space.domain,
                space.icon,
                space.color,
                space.description,
                space.created_at,
                space.updated_at,
            ),
        )

    @staticmethod
    def default_persona(space: Space) -> Persona:
        """按 Space 基本信息生成初始 Persona。"""
        return Persona(
            name=f"{space.name}专家",
            domain=space.domain,
            role_description=(
                f"你是 {space.domain} 领域的资深专家，长期在该领域一线工作。"
                "回答时只依据提供的资料与经验，资料不足时明确说明不知道。"
            ),
            principles=[
                "先给结论，再给依据",
                "任何结论都要能追溯到原文或经验条目",
                "资料中没有的内容不要编造",
            ],
            quality_bar=[
                "是否给出了具体、可执行的做法而非泛泛而谈",
                "是否标注了结论的适用范围与前提",
            ],
        )

    async def update_space(self, space_id: str, data: SpaceUpdate) -> Space:
        """更新 Space 基本信息（两个库同步），名称与领域同步进 ``space.yaml`` 的 persona。

        所有提示词（问答、领域大纲、出题、蒸馏）读的都是 persona.domain，不是 Space 记录。
        此前只改库不改 yaml：界面上领域改好了，大纲重新生成出来还是按旧领域。
        """
        await self.open()
        before = await self.global_db.spaces.get(space_id)
        space = await self.global_db.spaces.update(space_id, data)
        database = await self.space_db(space_id)
        await database.spaces.update(space_id, data)
        if before is not None and (before.domain != space.domain or before.name != space.name):
            await self._sync_persona_identity(space_id, before, space)
        return space

    async def _sync_persona_identity(self, space_id: str, before: Space, after: Space) -> None:
        """把改名 / 改领域带进 persona：领域总是跟着改；名称与角色描述只在仍是
        系统生成的默认文案时才重写，用户自己写过的不动。"""
        config = await self.read_config(space_id)
        persona = config.persona
        old_default = self.default_persona(before)
        new_default = self.default_persona(after)
        updates: dict[str, str] = {"domain": after.domain}
        if persona.name == old_default.name:
            updates["name"] = new_default.name
        if persona.role_description == old_default.role_description:
            updates["role_description"] = new_default.role_description
        config.persona = persona.model_copy(update=updates)
        await self.write_config(space_id, config)
        logger.info("persona_identity_synced", space_id=space_id, fields=sorted(updates))

    async def delete_space(self, space_id: str, *, purge: bool = True) -> bool:
        """删除 Space。

        ``purge=True``（默认）连同磁盘上的文档、向量与解析缓存一起删掉。
        默认必须是「真删」：留着一个注册表里已经没有、界面上也看不见的目录，
        用户以为资料删干净了，实际几个 GB 还躺在磁盘上——这既不符合直觉，
        也会让同 id 的空间再也导入不回来（目标目录已存在）。
        需要保留数据时显式传 ``purge=False``。
        """
        await self.open()
        await self.close_space(space_id)
        deleted = await self.global_db.spaces.delete(space_id)
        if purge:
            directory = self.space_dir(space_id)
            if directory.exists():
                await asyncio.to_thread(shutil.rmtree, directory, True)
        logger.info("space_deleted", space_id=space_id, purge=purge)
        return deleted

    # -- space.yaml -------------------------------------------------------

    async def read_config(self, space_id: str) -> SpaceYaml:
        """读取 Space 配置（含 L4 Persona）。

        ``space.yaml`` 缺失、或里面的领域是空的时候，用 Space 记录重建一份并落盘：
        空领域会让所有提示词变成「你是「」领域的资深专家」，领域大纲、卡片抽取、
        经验蒸馏全都跑不出东西——而界面上看不出任何异常。
        """
        path = self.yaml_path(space_id)
        config = await asyncio.to_thread(load_space_yaml, path)
        if config.persona.domain:
            return config

        space = await self.require_space(space_id)
        if not space.domain:
            return config
        healed = config.model_copy(update={"persona": self.default_persona(space)})
        await self.write_config(space_id, healed)
        logger.info(
            "space_config_rebuilt",
            space_id=space_id,
            domain=space.domain,
            reason="persona_domain_missing",
        )
        return healed

    async def write_config(self, space_id: str, config: SpaceYaml) -> SpaceYaml:
        """写回 Space 配置。"""
        self.init_space_dir(space_id)
        await asyncio.to_thread(save_space_yaml, self.yaml_path(space_id), config)
        return config

    async def update_persona(self, space_id: str, persona: Persona) -> SpaceYaml:
        """只更新 Persona 段。"""
        config = await self.read_config(space_id)
        config.persona = persona
        return await self.write_config(space_id, config)

    # -- 导入导出 ---------------------------------------------------------

    async def export_zip(self, space_id: str) -> bytes:
        """导出 Space 为 zip 字节流。"""
        await self.require_space(space_id)
        database = await self.space_db(space_id)
        await database.sqlite.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        await self.close_space(space_id)
        if not self.meta_db_path(space_id).is_file():
            raise ValidationError("该 Space 的 meta.db 不存在", detail={"space_id": space_id})
        return await asyncio.to_thread(export_space_zip, self.space_dir(space_id))

    async def export_to_file(self, space_id: str, destination: Path) -> Path:
        """导出 Space 到指定文件。"""
        payload = await self.export_zip(space_id)
        async with aiofiles.open(destination, "wb") as handle:
            await handle.write(payload)
        return destination

    async def import_zip(self, payload: bytes, *, name: str | None = None) -> Space:
        """从 zip 导入 Space。

        说明：若压缩包内的 space_id 已存在，则拒绝导入（避免覆盖既有数据）。
        """
        await self.open()
        staging = self.spaces_root / f".import-{id(payload):x}"
        if staging.exists():
            await asyncio.to_thread(shutil.rmtree, staging, True)
        try:
            await asyncio.to_thread(import_space_zip, payload, staging)
            database = Database(staging / META_DB_NAME, staging / VECTORS_DIR_NAME)
            await database.open()
            rows = await database.sqlite.fetchall("SELECT * FROM spaces LIMIT 1")
            if not rows:
                raise ValidationError("压缩包内的 meta.db 缺少 spaces 记录")
            space = Space.model_validate(dict(rows[0]))
            _require_valid_space_id(space.id)
            if name:
                space = space.model_copy(update={"name": name})
            await database.close()
            if await self.global_db.spaces.get(space.id) is not None:
                raise ValidationError(
                    "这个知识空间在本机已存在，不能重复导入（导入不会覆盖现有数据）",
                    detail={"space_id": space.id},
                )
            target = self.space_dir(space.id)
            if target.exists():
                # 注册表里没有它、目录却还在。最常见的成因确实是早先删除时没清磁盘，
                # 但**不能**据此断定它是垃圾就地删掉：注册表本身也可能因为 bug 丢行
                # （`_insert_space_row` 的 `DELETE FROM spaces WHERE id != ?` 就干过
                # 这件事，一次导入抹掉了注册表里其他所有 Space，数据却完好躺在磁盘上）。
                # 那种情况下直接 rmtree 就是把用户仅存的数据彻底销毁。
                # 所以改成移进回收目录：导入照常继续，原目录可回收，代价只是磁盘空间。
                recycled = self._recycle_dir(space.id)
                logger.warning(
                    "import_recycled_orphan_dir",
                    space_id=space.id,
                    path=str(target),
                    moved_to=str(recycled),
                )
                recycled.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(target.rename, recycled)
            await asyncio.to_thread(staging.rename, target)
        except Exception:
            if staging.exists():
                await asyncio.to_thread(shutil.rmtree, staging, True)
            raise
        await self._insert_space_row(self.global_db, space)
        await self._mirror_space(space)
        logger.info("space_imported", space_id=space.id, name=space.name)
        return space

    # -- 统计 -------------------------------------------------------------

    async def disk_usage_bytes(self) -> int:
        """数据目录占用的磁盘字节数。"""

        def measure() -> int:
            total = 0
            root = self.settings.data_dir
            if not root.exists():
                return 0
            for path in root.rglob("*"):
                if path.is_file():
                    try:
                        total += path.stat().st_size
                    except OSError:  # pragma: no cover - 文件被并发删除
                        continue
            return total

        return await asyncio.to_thread(measure)


#: ULID：26 位 Crockford base32，字典序即时间序
_SPACE_ID_PATTERN = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def _require_valid_space_id(space_id: str) -> None:
    """导入时校验 Space id 的形态。

    路径沙箱已经能挡住越界写入，但一个「合法目录名却完全不是 ULID」的 id
    仍会污染全局注册表、并让后续所有按 id 拼接的路径变得不可预期。
    不信任的输入就该按白名单收窄，而不是事后补救。
    """
    if not _SPACE_ID_PATTERN.match(space_id):
        raise ValidationError("压缩包内的 Space id 非法", detail={"length": len(space_id)})
