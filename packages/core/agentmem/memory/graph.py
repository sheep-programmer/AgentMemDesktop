"""知识图谱聚合：把实体与关系整理成前端力导向图能直接渲染的节点与边。

两件容易被大图拖垮的事在这里被封顶：

- 默认只返回提及次数最高的 ``limit`` 个节点，边只保留两端都在结果里的；
- 以某实体为中心时做 N 度邻居展开，只取该邻域，避免把整个 Space 的图吐出去。

``depth=1`` 是直接相连的邻居，``depth=2`` 是邻居的邻居，以此类推。

实体与关系都不做全量装载：没有中心点时 ``top_by_mention`` 一条 SQL 取够 ``limit``
个节点；有中心点时按层查邻居（每层一次 SQL），再按 id 取回邻域里那些实体。
边只查两端都留在图上的那些（``relations.between``），总数各自 COUNT 一次——
于是开销跟着「画出来的那部分」走，而不是跟着 Space 的大小走。
"""

from __future__ import annotations

from collections.abc import Sequence

import structlog

from agentmem.errors import NotFoundError
from agentmem.store import Database
from agentmem.types import Entity, GraphEdge, GraphNode, GraphResponse

logger = structlog.get_logger(__name__)


async def _resolve_center(database: Database, space_id: str, center: str) -> Entity:
    """把中心参数解析成实体：先按 id，再按名称（忽略大小写）。

    两次都走 SQL 而不是在已装载的实体里找：中心实体未必在「提及最多的那批」里，
    按名字点进来的冷门实体照样要能定位。

    Raises:
        NotFoundError: 该 Space 里找不到这个实体。
    """
    entity = await database.entities.get(center)
    if entity is not None and entity.space_id == space_id:
        return entity
    by_name = await database.entities.find_by_name(space_id, center)
    if by_name is None:
        raise NotFoundError("实体", center)
    return by_name


async def _neighborhood(database: Database, space_id: str, center: Entity, depth: int) -> list[str]:
    """从中心出发做 N 度广度优先展开，返回邻域内的实体 id（含中心自己，定序）。

    每层一次 SQL，而不是先把全 Space 的边读出来建邻接表：层数是个位数，边数却随
    Space 一直涨，后者迟早读不动。
    """
    visited = {center.id}
    frontier = {center.id}
    for _ in range(depth):
        reached = await database.relations.neighbors(space_id, sorted(frontier))
        reached -= visited
        if not reached:
            break
        visited |= reached
        frontier = reached
    return sorted(visited)


def _truncate(entities: Sequence[Entity], limit: int, pinned_id: str | None) -> list[Entity]:
    """按提及次数截断，并保证 ``pinned_id``（中心实体）一定在结果里。"""
    ordered = sorted(entities, key=lambda entity: (-entity.mention_count, entity.id))
    if len(ordered) <= limit:
        return ordered
    kept = ordered[:limit]
    if pinned_id is not None and all(entity.id != pinned_id for entity in kept):
        pinned = next(entity for entity in ordered if entity.id == pinned_id)
        kept = [*ordered[: limit - 1], pinned]
    return kept


async def build_graph(
    database: Database,
    space_id: str,
    *,
    center: str | None = None,
    depth: int = 2,
    limit: int = 300,
) -> GraphResponse:
    """构造实体关系图。

    Args:
        database: 该 Space 的存储门面。
        space_id: 所属 Space。
        center: 只取该实体周围 ``depth`` 度以内的节点；可以是 id 或名称。
        depth: 邻居展开的度数。
        limit: 节点数上限，按提及次数从高到低保留。

    Returns:
        节点权重是实体的提及次数，边权重来自关系自身。

    Raises:
        NotFoundError: 指定了 ``center`` 但该实体不存在。
    """
    total_entities = await database.entities.count(space_id)
    if total_entities == 0:
        return GraphResponse()

    if center:
        resolved = await _resolve_center(database, space_id, center)
        neighborhood = await _neighborhood(database, space_id, resolved, depth)
        selected = await database.entities.get_many(neighborhood)
        kept = _truncate(selected, limit, resolved.id)
        # 裁剪前的总量：图被截断时界面要说清楚，否则用户以为看到的就是全部
        total_nodes = len(selected)
    else:
        # 没有中心点时不必装载整个 Space：要画的就是提及最多的那 limit 个，
        # 交给 SQL 取；总量单独 COUNT 一次，用来告诉界面还有多少没画出来
        kept = await database.entities.top_by_mention(space_id, limit=limit)
        total_nodes = total_entities

    kept_ids = [entity.id for entity in kept]
    relations = await database.relations.between(space_id, kept_ids)
    total_edges = await database.relations.count(space_id)
    edges = [
        GraphEdge(
            src=relation.src_id,
            dst=relation.dst_id,
            predicate=relation.predicate,
            weight=relation.weight,
        )
        for relation in relations
    ]
    nodes = [
        GraphNode(
            id=entity.id, name=entity.name, type=entity.type, weight=float(entity.mention_count)
        )
        for entity in kept
    ]
    logger.debug(
        "graph_built",
        space_id=space_id,
        center=center,
        depth=depth,
        nodes=len(nodes),
        edges=len(edges),
    )
    return GraphResponse(
        nodes=nodes,
        edges=edges,
        total_nodes=total_nodes,
        total_edges=total_edges,
        truncated=len(kept) < total_nodes,
    )
