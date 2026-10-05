import type {
  KnowledgeMap,
  KnowledgeMapNode,
  KnowledgeMapTopic,
} from '@/lib/api/services/knowledgeMap';

/**
 * 知识图谱的前端模型：在后端给的网络之上补两样东西——
 *
 * - **待补槽位**（ghost）：一个主题要有 {@link CARD_SATURATION} 张卡片才算有厚度，
 *   差几张就挂几个灰色虚线小球。卡片补进来时，新卡片落在空出来的槽位上；
 * - **成长时间线**：主题是「该懂什么」的目标，从一开始就在；资料、卡片、经验按
 *   各自的创建时间一个个长出来，主题随之被填满。
 */

export type BaseKind = KnowledgeMapNode['kind'];
export type Kind = BaseKind | 'ghost';
export type LayoutMode = 'network' | 'topic' | 'type';

/** force-graph 会往节点上写坐标；保留原始字段，另加模拟用的位置。 */
export type GraphNode = Omit<KnowledgeMapNode, 'kind'> & {
  kind: Kind;
  /** 待补槽位所属的主题节点 id */
  topicId?: string;
  /** 待补槽位的序号（0 起） */
  slot?: number;
  x?: number;
  y?: number;
  vx?: number;
  vy?: number;
  fx?: number;
  fy?: number;
};
export type GraphLink = { source: string | GraphNode; target: string | GraphNode; kind: string };

/** 与后端 `knowledge_map.CARD_SATURATION` 一致：命中这么多张卡片，主题才算有厚度 */
export const CARD_SATURATION = 3;

export const KIND_LABEL: Record<Kind, string> = {
  domain: '领域',
  topic: '主题',
  document: '资料',
  card: '知识卡片',
  insight: '经验',
  ghost: '待补',
};

export const CARD_KIND_LABEL: Record<string, string> = {
  concept: '概念',
  fact: '事实',
  procedure: '规程',
  pitfall: '陷阱',
  tool: '工具',
};

export const IMPORTANCE_LABEL: Record<string, string> = {
  core: '核心',
  common: '常见',
  advanced: '进阶',
};

export const IMPORTANCE_ORDER: Record<string, number> = { core: 0, common: 1, advanced: 2 };

export const endId = (end: string | GraphNode) => (typeof end === 'string' ? end : end.id);

export interface Graph {
  nodes: GraphNode[];
  links: GraphLink[];
  /** 主题节点 id → 命中它的卡片节点（按后端顺序） */
  topicCards: Map<string, GraphNode[]>;
  /** 卡片节点 id → 它归属的第一个主题 */
  cardTopic: Map<string, string>;
  /** 主题节点 id → 主题统计 */
  topicInfo: Map<string, KnowledgeMapTopic>;
  neighbors: Map<string, Set<string>>;
  byId: Map<string, GraphNode>;
}

/**
 * 把响应变成图。传入上一版的节点时沿用它们的坐标：刷新、补上一张卡片之后
 * 整张图不会重新洗牌，只有新长出来的那几个点在动。
 */
export function buildGraph(data: KnowledgeMap | null, previous?: Map<string, GraphNode>): Graph {
  const nodes: GraphNode[] = [];
  const links: GraphLink[] = [];
  const topicCards = new Map<string, GraphNode[]>();
  const cardTopic = new Map<string, string>();
  const topicInfo = new Map<string, KnowledgeMapTopic>();
  const byId = new Map<string, GraphNode>();

  const keep = (node: GraphNode) => {
    const old = previous?.get(node.id);
    if (old && typeof old.x === 'number') {
      node.x = old.x;
      node.y = old.y;
      node.vx = 0;
      node.vy = 0;
    }
    nodes.push(node);
    byId.set(node.id, node);
  };

  const source = data?.nodes ?? [];
  const topicNodes = source.filter((node) => node.kind === 'topic');
  const topics = data?.topics ?? [];
  topicNodes.forEach((node, index) => {
    const info = topics.find((topic) => topic.topic === node.label) ?? topics[index];
    if (info) topicInfo.set(node.id, info);
  });

  // 初始位置：领域钉在原点，主题绕一圈，待补槽位挂在各自主题外侧
  const ring = 90 + topicNodes.length * 7;
  const angleOf = new Map<string, number>();
  topicNodes.forEach((node, index) => {
    angleOf.set(node.id, (index / Math.max(1, topicNodes.length)) * Math.PI * 2 - Math.PI / 2);
  });

  for (const raw of source) {
    const node: GraphNode = { ...raw };
    if (node.kind === 'domain') {
      node.x = 0;
      node.y = 0;
      node.fx = 0;
      node.fy = 0;
    } else if (node.kind === 'topic') {
      const angle = angleOf.get(node.id) ?? 0;
      node.x = Math.cos(angle) * ring;
      node.y = Math.sin(angle) * ring;
    }
    keep(node);
  }

  for (const edge of data?.edges ?? []) {
    links.push({ source: edge.src, target: edge.dst, kind: edge.kind });
    if (edge.kind === 'covers' && edge.src.startsWith('topic:')) {
      const card = byId.get(edge.dst);
      if (!card) continue;
      if (!topicCards.has(edge.src)) topicCards.set(edge.src, []);
      topicCards.get(edge.src)!.push(card);
      if (!cardTopic.has(edge.dst)) cardTopic.set(edge.dst, edge.src);
    }
  }

  // 待补槽位：每个主题固定 CARD_SATURATION 个，按时间轴决定还剩几个空着
  for (const topic of topicNodes) {
    const info = topicInfo.get(topic.id);
    const subtopics = info?.subtopics ?? [];
    const angle = angleOf.get(topic.id) ?? 0;
    for (let slot = 0; slot < CARD_SATURATION; slot += 1) {
      const ghost: GraphNode = {
        id: `ghost:${topic.id}:${slot}`,
        kind: 'ghost',
        label: subtopics[slot] ?? `${topic.label} · 待补`,
        weight: 0,
        mastery: null,
        // 有对应子主题的槽位标 subtopic，标签就是建议补的内容；否则只是「还差一张」
        status: subtopics[slot] ? 'subtopic' : 'slot',
        topicId: topic.id,
        slot,
        detail: null,
      };
      const spread = (slot - (CARD_SATURATION - 1) / 2) * 0.32;
      ghost.x = Math.cos(angle + spread) * (ring + 48);
      ghost.y = Math.sin(angle + spread) * (ring + 48);
      keep(ghost);
      links.push({ source: topic.id, target: ghost.id, kind: 'ghost' });
    }
  }

  const neighbors = new Map<string, Set<string>>();
  for (const link of links) {
    const a = endId(link.source);
    const b = endId(link.target);
    if (!neighbors.has(a)) neighbors.set(a, new Set());
    if (!neighbors.has(b)) neighbors.set(b, new Set());
    neighbors.get(a)!.add(b);
    neighbors.get(b)!.add(a);
  }

  return { nodes, links, topicCards, cardTopic, topicInfo, neighbors, byId };
}

/** 节点在时间轴上是否「已经出现」。主题与领域是目标本身，一开始就在。 */
export function bornBy(node: GraphNode, cursor: number | null): boolean {
  if (cursor === null) return true;
  if (node.kind === 'domain' || node.kind === 'topic' || node.kind === 'ghost') return true;
  return typeof node.created_at !== 'number' || node.created_at <= cursor;
}

/** 某个时刻命中该主题的卡片数；「现在」用后端的计数（画布上限之外的卡片也算）。 */
export function topicCountAt(graph: Graph, topicId: string, cursor: number | null): number {
  if (cursor === null) {
    return graph.topicInfo.get(topicId)?.card_count ?? graph.topicCards.get(topicId)?.length ?? 0;
  }
  return (graph.topicCards.get(topicId) ?? []).filter((card) => bornBy(card, cursor)).length;
}

/**
 * 某个时刻的主题掌握度。「现在」直接用后端算好的值；回放时按当时已有的卡片
 * 用同一个公式重算（`min(1, n/3) × 平均置信度`），主题就会随卡片一点点被填满。
 */
export function topicMasteryAt(graph: Graph, topicId: string, cursor: number | null): number {
  if (cursor === null) return graph.byId.get(topicId)?.mastery ?? 0;
  const cards = (graph.topicCards.get(topicId) ?? []).filter((card) => bornBy(card, cursor));
  if (cards.length === 0) return 0;
  const quality = cards.reduce((sum, card) => sum + (card.mastery ?? 0), 0) / cards.length;
  return Math.min(1, cards.length / CARD_SATURATION) * quality;
}

/** 回放的节拍：每个有内容出现的时刻算一拍，按拍子匀速推进，而不是按真实时间 */
export function buildSteps(data: KnowledgeMap | null): number[] {
  const stamps = new Set<number>();
  for (const node of data?.nodes ?? []) {
    if (node.kind === 'domain' || node.kind === 'topic') continue;
    if (typeof node.created_at === 'number') stamps.add(node.created_at);
  }
  for (const milestone of data?.milestones ?? []) stamps.add(milestone.at);
  return [...stamps].sort((a, b) => a - b);
}

export function nodeRadius(node: GraphNode): number {
  switch (node.kind) {
    case 'domain':
      return 16;
    case 'topic':
      return 10 + node.weight * 5;
    case 'document':
      return 7;
    case 'insight':
      return 6.5;
    case 'ghost':
      return 9;
    default:
      return 3 + node.weight * 2.8;
  }
}

/** 标签摆放顺序：悬停/选中的先占位，其次领域、主题、资料、经验、卡片 */
export function labelPriority(node: GraphNode, hoverId: string | null, selectedId: string | null): number {
  if (node.id === hoverId || node.id === selectedId) return 0;
  const order: Record<string, number> = { domain: 1, topic: 2, document: 3, insight: 4, card: 5, ghost: 6 };
  return order[node.kind] ?? 7;
}

// -- 分组布局 ------------------------------------------------------------------

export const CLUSTER_LABEL: Record<string, string> = {
  topics: '大纲主题',
  docs: '资料',
  insights: '经验',
  other: '未归入主题',
  'card:concept': '概念',
  'card:fact': '事实',
  'card:procedure': '规程',
  'card:pitfall': '陷阱',
  'card:tool': '工具',
};

/** 节点在当前布局下归哪一组；`null` 表示不参与分组（领域、或「关系」布局） */
export function clusterKey(node: GraphNode, mode: LayoutMode, graph: Graph): string | null {
  if (mode === 'network' || node.kind === 'domain') return null;
  if (node.kind === 'document') return 'docs';
  if (node.kind === 'insight') return 'insights';
  if (mode === 'topic') {
    if (node.kind === 'topic') return node.id;
    if (node.kind === 'ghost') return node.topicId ?? null;
    return graph.cardTopic.get(node.id) ?? 'other';
  }
  if (node.kind === 'topic' || node.kind === 'ghost') return 'topics';
  return `card:${node.status ?? 'fact'}`;
}

/**
 * 各组的中心：沿一个圆按组的大小分角度，组越大占的弧越长、圆也越大。
 * 「按类型」时大纲主题那一组放在中间，围着领域。
 */
export function clusterCenters(
  nodes: GraphNode[],
  mode: LayoutMode,
  graph: Graph,
): Map<string, { x: number; y: number }> {
  const centers = new Map<string, { x: number; y: number }>();
  if (mode === 'network') return centers;
  const sizes = new Map<string, number>();
  for (const node of nodes) {
    const key = clusterKey(node, mode, graph);
    if (key) sizes.set(key, (sizes.get(key) ?? 0) + 1);
  }
  const order = (key: string) => {
    if (key.startsWith('topic:')) return Number(key.slice(6));
    const fixed = ['topics', 'card:concept', 'card:fact', 'card:procedure', 'card:pitfall', 'card:tool', 'other', 'docs', 'insights'];
    const index = fixed.indexOf(key);
    return 1000 + (index < 0 ? fixed.length : index);
  };
  const keys = [...sizes.keys()].sort((a, b) => order(a) - order(b));
  const ringKeys = mode === 'type' ? keys.filter((key) => key !== 'topics') : keys;
  if (mode === 'type' && sizes.has('topics')) centers.set('topics', { x: 0, y: 0 });
  const diameter = (key: string) => 2 * (38 + Math.sqrt(sizes.get(key) ?? 1) * 18);
  const total = ringKeys.reduce((sum, key) => sum + diameter(key), 0);
  const inner = mode === 'type' ? 150 + Math.sqrt(sizes.get('topics') ?? 0) * 18 : 0;
  const radius = Math.max(inner + 140, (total * 1.3) / (Math.PI * 2));
  let angle = -Math.PI / 2;
  for (const key of ringKeys) {
    const share = (diameter(key) / Math.max(1, total)) * Math.PI * 2;
    const middle = angle + share / 2;
    centers.set(key, { x: Math.cos(middle) * radius, y: Math.sin(middle) * radius });
    angle += share;
  }
  return centers;
}

// -- 自定义力 --------------------------------------------------------------------

type Force = ((alpha: number) => void) & { initialize: (nodes: GraphNode[]) => void };

/** 极简的碰撞力：两两推开重叠的节点。图谱最多几百个节点，两两比较足够快。 */
export function collideForce(radius: (node: GraphNode) => number): Force {
  let nodes: GraphNode[] = [];
  const force = ((alpha: number) => {
    for (let i = 0; i < nodes.length; i += 1) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j += 1) {
        const b = nodes[j];
        let dx = (b.x ?? 0) - (a.x ?? 0);
        let dy = (b.y ?? 0) - (a.y ?? 0);
        if (dx === 0 && dy === 0) {
          const angle = hashUnit(a.id + b.id) * Math.PI * 2;
          dx = Math.cos(angle) * 0.01;
          dy = Math.sin(angle) * 0.01;
        }
        const distance = Math.hypot(dx, dy);
        const overlap = radius(a) + radius(b) - distance;
        if (overlap <= 0) continue;
        const push = (overlap / distance) * 0.5 * alpha;
        if (a.fx == null) a.vx = (a.vx ?? 0) - dx * push;
        if (a.fy == null) a.vy = (a.vy ?? 0) - dy * push;
        if (b.fx == null) b.vx = (b.vx ?? 0) + dx * push;
        if (b.fy == null) b.vy = (b.vy ?? 0) + dy * push;
      }
    }
  }) as Force;
  force.initialize = (next) => {
    nodes = next;
  };
  return force;
}

/** 把节点拉向所在组的中心；`center` 返回空的节点不受影响 */
export function clusterForce(
  center: (node: GraphNode) => { x: number; y: number } | null,
  strength: (node: GraphNode) => number,
): Force {
  let nodes: GraphNode[] = [];
  const force = ((alpha: number) => {
    for (const node of nodes) {
      const target = center(node);
      if (!target) continue;
      const k = strength(node) * alpha;
      node.vx = (node.vx ?? 0) + (target.x - (node.x ?? 0)) * k;
      node.vy = (node.vy ?? 0) + (target.y - (node.y ?? 0)) * k;
    }
  }) as Force;
  force.initialize = (next) => {
    nodes = next;
  };
  return force;
}

// -- 小工具 ----------------------------------------------------------------------

/** 时间轴与回放使用同一套节拍，集中导入的知识也有足够的拖动空间。 */
export function stepIndexAt(steps: number[], cursor: number | null): number {
  if (cursor === null) return steps.length;
  let low = 0;
  let high = steps.length;
  while (low < high) {
    const middle = (low + high) >>> 1;
    if (steps[middle] <= cursor) low = middle + 1;
    else high = middle;
  }
  return low;
}

export function cursorAtStep(steps: number[], index: number): number | null {
  if (steps.length === 0 || index >= steps.length) return null;
  return index <= 0 ? steps[0] - 1 : steps[Math.floor(index) - 1];
}

export function isSolid(count: number, mastery: number): boolean {
  return count >= CARD_SATURATION && mastery >= 0.75;
}

export function formatDate(ms: number): string {
  const date = new Date(ms);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

export function percent(value?: number | null): string {
  return typeof value === 'number' ? `${Math.round(value * 100)}` : '—';
}

/** 稳定的 0~1 伪随机数：同一个 id 每次都一样，让呼吸、流动错开相位 */
export function hashUnit(id: string): number {
  let hash = 2166136261;
  for (let i = 0; i < id.length; i += 1) {
    hash ^= id.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return ((hash >>> 0) % 10000) / 10000;
}
