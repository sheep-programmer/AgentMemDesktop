"""命令行入口：``agentmem serve`` 启动后端服务，``agentmem doctor`` 体检数据。"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

import uvicorn

from agentmem.config import Settings, get_settings
from agentmem.space.manager import SpaceManager


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(prog="agentmem", description="AgentMem 后端服务")
    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve", help="启动 HTTP 服务")
    serve.add_argument("--host", default=None, help="监听地址，缺省读 AGENTMEM_HOST")
    serve.add_argument("--port", type=int, default=None, help="监听端口，缺省读 AGENTMEM_PORT")
    serve.add_argument("--reload", action="store_true", help="开发模式自动重载")
    serve.add_argument("--log-level", default=None, help="日志级别")

    subparsers.add_parser("config", help="打印当前生效的配置")

    demo = subparsers.add_parser("demo", help="铺一个带示例数据的 Space")
    demo.add_argument("--name", default=None, help="示例 Space 的名字")

    recover = subparsers.add_parser("recover", help="重新登记磁盘上有、注册表里却没有的 Space")
    recover.add_argument(
        "--apply",
        action="store_true",
        help="真的写回注册表；缺省只列出发现了什么（dry-run）",
    )

    doctor = subparsers.add_parser("doctor", help="体检各 Space 的派生数据，清掉老版本留下的孤儿")
    doctor.add_argument("--space", default=None, metavar="SPACE_ID", help="只查这一个 Space")
    doctor.add_argument(
        "--apply",
        action="store_true",
        help="真的清理；缺省只列出发现了什么（dry-run）",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = get_settings()

    if args.command == "recover":
        return recover_spaces(settings, apply=args.apply)

    if args.command == "doctor":
        return doctor(settings, space_id=args.space, apply=args.apply)

    if args.command == "demo":
        return seed_demo(settings, name=args.name)

    if args.command == "config":
        for key, value in settings.model_dump().items():
            print(f"{key} = {value}")  # noqa: T201 - CLI 输出
        return 0

    if args.command in (None, "serve"):
        return serve(
            settings, host=args.host, port=args.port, reload=args.reload, log_level=args.log_level
        )

    parser.print_help()
    return 1


def seed_demo(settings: Settings | None = None, *, name: str | None = None) -> int:
    """铺设示例 Space：让第一次打开应用的人立刻看到有内容的界面。"""
    import asyncio

    from agentmem.space.demo import DEMO_SPACE_NAME, seed_demo_space
    from agentmem.space.runtime import Runtime
    from apps.api.main import create_app

    resolved = settings or get_settings()
    app = create_app(resolved)

    async def run() -> None:
        # runtime 由 lifespan 建起来，所以必须走一遍 lifespan，不能直接取 app.state
        async with app.router.lifespan_context(app):
            runtime: Runtime = app.state.runtime
            space = await seed_demo_space(runtime, name=name or DEMO_SPACE_NAME)
            print(f"示例 Space 就绪：{space.name}")  # noqa: T201 - CLI 输出
            print(f"  id = {space.id}")  # noqa: T201 - CLI 输出
            print("  启动服务后打开浏览器即可看到它。")  # noqa: T201 - CLI 输出

    asyncio.run(run())
    return 0


def recover_spaces(settings: Settings, *, apply: bool = False) -> int:
    """重新登记孤儿 Space 目录。

    注册表丢一行，这个 Space 就在界面与 API 里彻底消失，而数据完好躺在磁盘上。
    这种事发生过：导入备份时 `DELETE FROM spaces WHERE id != ?` 抹掉了注册表里
    其他所有 Space（已修），当时只能手写 SQL 救回来。这个命令把那条路铺进产品里。

    默认 dry-run：先让用户看清要恢复什么，确认无误再 ``--apply``。
    """

    async def run() -> int:
        manager = SpaceManager(settings)
        try:
            found = await manager.find_unregistered_space_dirs()
            if not found:
                print("没有发现未登记的 Space 目录。")  # noqa: T201 - CLI 输出
                return 0
            print(f"发现 {len(found)} 个未登记的 Space：")  # noqa: T201 - CLI 输出
            for space_id, name in found:
                print(f"  {space_id}  {name}")  # noqa: T201 - CLI 输出
            if not apply:
                print("\n这是 dry-run。确认无误后加 --apply 写回注册表。")  # noqa: T201
                return 0
            for space_id, _name in found:
                space = await manager.reregister_space_dir(space_id)
                print(f"已登记：{space.id}  {space.name}")  # noqa: T201 - CLI 输出
            return 0
        finally:
            await manager.close()

    return asyncio.run(run())


def doctor(settings: Settings, *, space_id: str | None = None, apply: bool = False) -> int:
    """逐个 Space 体检派生数据，``apply`` 时清理。

    老版本删文档不清派生数据（已修），老库里留着孤儿卡片、孤立实体、没人引用的原始文件
    和回不了表的向量。它们不会报错，只会安静地污染检索和图谱，所以做成一个命令，
    默认 dry-run：先看清要删什么，确认无误再 ``--apply``。
    """
    from agentmem.space.doctor import diagnose, repair

    async def run() -> int:
        manager = SpaceManager(settings)
        try:
            if space_id is not None:
                space = await manager.get_space(space_id)
                if space is None:
                    print(f"没有这个 Space：{space_id}")  # noqa: T201 - CLI 输出
                    return 1
                spaces = [space]
            else:
                spaces = await manager.list_spaces()
            if not spaces:
                print("没有任何 Space。")  # noqa: T201 - CLI 输出
                return 0
            dirty = 0
            for space in spaces:
                database = await manager.space_db(space.id)
                report = await diagnose(
                    database,
                    space_id=space.id,
                    raw_dir=manager.raw_dir(space.id),
                    cache_dir=settings.data_dir / "cache" / space.id,
                )
                print(f"[{space.name}]  {space.id}")  # noqa: T201 - CLI 输出
                if report.is_clean:
                    print("  未发现问题。")  # noqa: T201 - CLI 输出
                    continue
                dirty += 1
                for label, count in report.findings():
                    print(f"  {count:>6}  {label}")  # noqa: T201 - CLI 输出
                if apply:
                    result = await repair(database, report)
                    print(  # noqa: T201 - CLI 输出
                        "  已清理："
                        f"删卡片 {result.cards_deleted}、修卡片来源 {result.cards_trimmed}、"
                        f"删关系 {result.relations_deleted}、"
                        f"修关系来源 {result.relations_trimmed}、"
                        f"删实体 {result.entities_deleted}、删向量 {result.vectors_deleted}、"
                        f"删原始文件 {result.raw_files_deleted}、"
                        f"删解析缓存 {result.parse_caches_deleted}"
                    )
            if dirty and not apply:
                print(  # noqa: T201 - CLI 输出
                    "\n这是 dry-run，什么都没改。确认无误后加 --apply 清理"
                    "（建议先停掉后端，并备份 data/ 目录）。"
                )
            return 0
        finally:
            await manager.close()

    return asyncio.run(run())


def serve(
    settings: Settings | None = None,
    *,
    host: str | None = None,
    port: int | None = None,
    reload: bool = False,
    log_level: str | None = None,
) -> int:
    """启动 uvicorn。"""
    resolved = settings or get_settings()
    resolved.ensure_dirs()
    uvicorn.run(
        "apps.api.main:app",
        host=host or resolved.host,
        port=port or resolved.port,
        reload=reload,
        log_level=(log_level or resolved.log_level).lower(),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
