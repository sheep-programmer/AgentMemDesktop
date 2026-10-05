import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import ForceGraph2D, { type ForceGraphMethods } from 'react-force-graph-2d';
import { toast } from 'sonner';
import {
  Network,
  RefreshCw,
  Play,
  Pause,
  FileText,
  Layers,
  Lightbulb,
  Target,
  Sparkles,
  TrendingUp,
  X,
  ArrowUpRight,
  Maximize2,
  ZoomIn,
  ZoomOut,
  Shapes,
  CircleDashed,
  SkipForward,
  Wand2,
  Loader2,
} from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Switch } from '@/components/ui/switch';
import { FourStateView } from '@/components/shared/FourStateView';
import { useSpaceStore } from '@/stores/useSpaceStore';
import { useUiStore } from '@/stores/useUiStore';
import { useThemeStore } from '@/stores/useThemeStore';
import { useMediaQuery } from '@/hooks/useMediaQuery';
import { cn } from '@/lib/utils';
import { knowledgeMapService, type KnowledgeMap, type KnowledgeMapMilestone } from '@/lib/api/services/knowledgeMap';
import { expertiseService } from '@/lib/api/services/expertise';
import {
  CARD_KIND_LABEL,
  CARD_SATURATION,
  IMPORTANCE_LABEL,
  IMPORTANCE_ORDER,
  KIND_LABEL,
  bornBy,
  buildGraph,
  buildSteps,
  clusterCenters,
  clusterForce,
  clusterKey,
  collideForce,
  cursorAtStep,
  endId,
  formatDate,
  nodeRadius,
  isSolid,
  percent,
  stepIndexAt,
  topicCountAt,
  topicMasteryAt,
  type GraphLink,
  type GraphNode,
  type Kind,
  type LayoutMode,
} from './graphModel';
import {
  cardKindColor,
  masteryColor,
  paintClusters,
  paintLabels,
  paintLink,
  paintNode,
  paintPointerArea,
  readPalette,
  type PaintEnv,
  type Palette,
} from './graphPaint';
import { GapFillPanel, SlotDots } from './GapFillPanel';

const MILESTONE_ICON: Record<KnowledgeMapMilestone['kind'], typeof FileText> = {
  document: FileText,
  evolution: Sparkles,
  snapshot: TrendingUp,
  insight: Lightbulb,
};

const KIND_FILTERS: { kind: Kind; label: string; icon: typeof FileText }[] = [
  { kind: 'topic', label: '主题', icon: Target },
  { kind: 'ghost', label: '待补', icon: CircleDashed },
  { kind: 'document', label: '资料', icon: FileText },
  { kind: 'card', label: '卡片', icon: Layers },
  { kind: 'insight', label: '经验', icon: Lightbulb },
];

const LAYOUTS: { mode: LayoutMode; label: string; icon: typeof FileText; hint: string }[] = [
  { mode: 'network', label: '关系', icon: Network, hint: '按来源与归属自然展开' },
  { mode: 'topic', label: '按主题', icon: Target, hint: '每个大纲主题一组，看哪一块还空着' },
  { mode: 'type', label: '按类型', icon: Shapes, hint: '概念 / 事实 / 规程 / 陷阱 / 工具 各成一组' },
];

const LAYOUT_STORAGE_KEY = 'agentmem-graph-layout';
const INTRO_STORAGE_KEY = 'agentmem-graph-intro';

const clamp = (value: number, min: number, max: number) => Math.max(min, Math.min(max, value));

/** 数字从旧值平滑滚到新值 */
function useCountUp(value: number | null, duration = 700): number | null {
  const [shown, setShown] = useState(value);
  const fromRef = useRef(value ?? 0);
  useEffect(() => {
    if (value === null) {
      setShown(null);
      fromRef.current = 0;
      return;
    }
    if (duration === 0) {
      setShown(value);
      fromRef.current = value;
      return;
    }
    const from = fromRef.current;
    const started = performance.now();
    let frame = 0;
    const tick = (now: number) => {
      const t = Math.min(1, (now - started) / duration);
      const next = from + (value - from) * (1 - (1 - t) ** 3);
      fromRef.current = next;
      setShown(next);
      if (t < 1) frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [value, duration]);
  return shown;
}

function introSeen(spaceId: string): boolean {
  try {
    return (sessionStorage.getItem(INTRO_STORAGE_KEY) ?? '').split(',').includes(spaceId);
  } catch {
    return true;
  }
}

function markIntroSeen(spaceId: string) {
  try {
    const seen = (sessionStorage.getItem(INTRO_STORAGE_KEY) ?? '').split(',').filter(Boolean);
    sessionStorage.setItem(INTRO_STORAGE_KEY, [...seen, spaceId].join(','));
  } catch {
    /* 隐私模式下存不了就每次都播 */
  }
}

const KIND_ORDER: Record<Kind, number> = { domain: 0, topic: 1, ghost: 2, document: 3, card: 4, insight: 5 };

/**
 * 知识图谱页：把这个空间的知识画成一张网，回答三个问题——
 *
 * 1. **懂了哪些、还缺哪些**：领域大纲里的每个主题是一个球，按掌握度从底部灌满；
 *    一个主题要 {@link CARD_SATURATION} 张卡片才算扎实，差几张就挂几个灰色虚线小球，
 *    点一下就能当场补上（手写一张卡片，或去导入资料）；
 * 2. **知识从哪来**：资料 → 卡片 → 经验的来源连线，可以按主题、按卡片类型分组看；
 * 3. **怎么长出来的**：第一次打开时图从零长出来——先是该懂的主题骨架，再按时间
 *    一份份资料、一张张卡片补进去；底部时间轴可以随时回放、拖到任意时刻。
 *
 * 数据来自 `GET /spaces/{id}/knowledge-map`，纯 SQL 聚合，不调用任何生成模型。
 */
export function KnowledgeMapPage() {
  const { spaceId: paramSpaceId } = useParams();
  const navigate = useNavigate();
  const currentSpaceId = useSpaceStore((state) => state.currentSpaceId);
  const spaceId = paramSpaceId || currentSpaceId;
  const openReader = useUiStore((state) => state.openReader);
  const { resolvedTheme } = useThemeStore();
  const isWide = useMediaQuery('(min-width: 1024px)');
  const reduced = useMediaQuery('(prefers-reduced-motion: reduce)');
  const [pixelRatio, setPixelRatio] = useState(() => window.devicePixelRatio || 1);

  useEffect(() => {
    const query = window.matchMedia(`(resolution: ${pixelRatio}dppx)`);
    const update = () => setPixelRatio(window.devicePixelRatio || 1);
    query.addEventListener('change', update);
    window.addEventListener('resize', update);
    return () => {
      query.removeEventListener('change', update);
      window.removeEventListener('resize', update);
    };
  }, [pixelRatio]);

  const [data, setData] = useState<KnowledgeMap | null>(null);
  const [status, setStatus] = useState<'loading' | 'ready' | 'error' | 'empty'>('loading');
  const [error, setError] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  const [refreshing, setRefreshing] = useState(false);
  const [outlineBusy, setOutlineBusy] = useState(false);

  const [hidden, setHidden] = useState<Set<Kind>>(new Set());
  const [showLabels, setShowLabels] = useState(false);
  const [layout, setLayout] = useState<LayoutMode>(() => {
    const saved = typeof localStorage !== 'undefined' ? localStorage.getItem(LAYOUT_STORAGE_KEY) : null;
    return saved === 'topic' || saved === 'type' ? saved : 'network';
  });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [hoverId, setHoverId] = useState<string | null>(null);
  const [timeCursor, setTimeCursor] = useState<number | null>(null);
  const [playing, setPlaying] = useState(false);
  const [playMode, setPlayMode] = useState<'intro' | 'replay'>('replay');
  const [topicFilter, setTopicFilter] = useState<'all' | 'gaps'>('all');

  const containerRef = useRef<HTMLDivElement>(null);
  const tooltipRef = useRef<HTMLDivElement>(null);
  const graphRef = useRef<ForceGraphMethods<GraphNode, GraphLink> | undefined>(undefined);
  const [size, setSize] = useState({ width: 800, height: 600 });
  const [palette, setPalette] = useState<Palette>(() => readPalette(null, false));

  // 跨渲染保留的动画状态
  const loadedSpaceRef = useRef<string | null>(null);
  const prevNodesRef = useRef<Map<string, GraphNode> | undefined>(undefined);
  const prevVisibleRef = useRef<Set<string>>(new Set());
  const bornRef = useRef<Map<string, number>>(new Map());
  const filledRef = useRef<Set<string>>(new Set());
  const levelRef = useRef<Map<string, number>>(new Map());
  const targetsRef = useRef<Map<string, number>>(new Map());
  const centersRef = useRef<Map<string, { x: number; y: number }>>(new Map());
  const lastFrameRef = useRef(0);
  const fitPendingRef = useRef(true);
  const cameraManualRef = useRef(false);
  const mapSignatureRef = useRef('');
  const envRef = useRef<PaintEnv>({
    palette,
    now: 0,
    reduced,
    layout,
    focusSet: null,
    focusId: null,
    hoverId: null,
    selectedId: null,
    born: bornRef.current,
    filled: filledRef.current,
    level: levelRef.current,
    topicCount: new Map(),
    cluster: new Map(),
    clusterColor: () => ({ color: palette.muted, dashed: false }),
    visible: [],
    showLabels: false,
    hulls: new Map(),
  });

  useEffect(() => {
    setPalette(readPalette(containerRef.current, resolvedTheme === 'dark'));
  }, [resolvedTheme, status]);

  useEffect(() => {
    localStorage.setItem(LAYOUT_STORAGE_KEY, layout);
  }, [layout]);

  // 加载：首次进入（或换了空间）整页 loading 并播放生长动画；之后的刷新原地更新
  useEffect(() => {
    if (!spaceId) return;
    const controller = new AbortController();
    const fresh = loadedSpaceRef.current !== spaceId;
    if (fresh) {
      setStatus('loading');
      setSelectedId(null);
      setTimeCursor(null);
      setPlaying(false);
      prevNodesRef.current = undefined;
      prevVisibleRef.current = new Set();
      bornRef.current.clear();
      filledRef.current.clear();
      levelRef.current.clear();
      envRef.current.hulls.clear();
      mapSignatureRef.current = '';
      cameraManualRef.current = false;
      fitPendingRef.current = true;
    } else {
      setRefreshing(true);
    }
    knowledgeMapService
      .getKnowledgeMap(spaceId, { signal: controller.signal })
      .then((map) => {
        if (controller.signal.aborted) return;
        loadedSpaceRef.current = spaceId;
        const signature = JSON.stringify(map);
        if (mapSignatureRef.current !== signature) {
          mapSignatureRef.current = signature;
          setData(map);
          if (!cameraManualRef.current) fitPendingRef.current = true;
        }
        setStatus((map.nodes ?? []).length === 0 ? 'empty' : 'ready');
        const steps = buildSteps(map);
        if (fresh && !reduced && steps.length > 0 && !introSeen(spaceId)) {
          markIntroSeen(spaceId);
          setTimeCursor(steps[0] - 1);
          setPlayMode('intro');
          setPlaying(true);
        }
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        if (fresh) {
          setError((err as Error)?.message || '知识图谱加载失败');
          setStatus('error');
        } else {
          toast.error((err as Error)?.message || '刷新失败');
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setRefreshing(false);
      });
    return () => controller.abort();
    // reduced 只影响要不要播开场，不该因为它重新拉数据
  }, [spaceId, reloadToken]);

  // 导入、抽取、进化都可能在其他页面发生；回到图谱时更新，停留时缓慢检查新知识。
  useEffect(() => {
    if (status !== 'ready' || playing || refreshing) return;
    const refresh = () => {
      if (document.visibilityState === 'visible') setReloadToken((token) => token + 1);
    };
    const timer = window.setInterval(refresh, 30_000);
    window.addEventListener('focus', refresh);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener('focus', refresh);
    };
  }, [status, playing, refreshing]);

  useEffect(() => {
    const element = containerRef.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect;
      setSize({ width: Math.max(1, width), height: Math.max(1, height) });
      // 画布尺寸变了，旧的平移与缩放已经不适用（尤其是桌面切换到手机宽度）。
      cameraManualRef.current = false;
      fitPendingRef.current = true;
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [status]);

  // 节点对象按 id 沿用上一版的坐标：刷新、补卡之后图不会重新洗牌
  const graph = useMemo(() => buildGraph(data, prevNodesRef.current), [data]);
  useEffect(() => {
    prevNodesRef.current = graph.byId;
  }, [graph]);

  const steps = useMemo(() => buildSteps(data), [data]);
  const timeRange = useMemo(() => {
    if (steps.length === 0) return null;
    return { min: steps[0], max: Math.max(steps[steps.length - 1], steps[0] + 1) };
  }, [steps]);
  const atStart = timeCursor !== null && timeRange !== null && timeCursor < timeRange.min;
  const cursorValue = timeCursor ?? timeRange?.max ?? 0;
  const cursorStep = stepIndexAt(steps, timeCursor);

  /** 各主题此刻的卡片数与掌握度 */
  const topicState = useMemo(() => {
    const count = new Map<string, number>();
    const mastery = new Map<string, number>();
    for (const node of graph.nodes) {
      if (node.kind !== 'topic') continue;
      count.set(node.id, topicCountAt(graph, node.id, timeCursor));
      mastery.set(node.id, topicMasteryAt(graph, node.id, timeCursor));
    }
    const values = [...mastery.values()];
    const overall =
      values.length === 0
        ? null
        : timeCursor === null && typeof data?.stats.overall === 'number'
          ? data.stats.overall
          : values.reduce((sum, value) => sum + value, 0) / values.length;
    return { count, mastery, overall };
  }, [graph, timeCursor, data]);

  // 可见集合：用 id 串做缓存键，集合不变时不给 force-graph 新对象（否则每一拍都会重新加热布局）
  const visibleKey = useMemo(() => {
    return graph.nodes
      .filter((node) => {
        if (node.kind === 'ghost') {
          if (hidden.has('ghost') || hidden.has('topic')) return false;
          return (node.slot ?? 0) >= (topicState.count.get(node.topicId ?? '') ?? 0);
        }
        if (node.kind !== 'domain' && hidden.has(node.kind)) return false;
        return bornBy(node, timeCursor);
      })
      .map((node) => node.id)
      .join('|');
  }, [graph, hidden, timeCursor, topicState]);

  const visibleData = useMemo(() => {
    const ids = new Set(visibleKey ? visibleKey.split('|') : []);
    const nodes = graph.nodes.filter((node) => ids.has(node.id));
    const links = graph.links.filter(
      (link) => ids.has(endId(link.source)) && ids.has(endId(link.target)),
    );

    // 新出现的节点：登记出生时刻，并从「父节点」那里长出来
    const previous = prevVisibleRef.current;
    const fresh = nodes
      .filter((node) => !previous.has(node.id))
      .sort((a, b) => KIND_ORDER[a.kind] - KIND_ORDER[b.kind]);
    const vanished = new Map<string, GraphNode[]>();
    for (const id of previous) {
      if (ids.has(id) || !id.startsWith('ghost:')) continue;
      const ghost = graph.byId.get(id);
      if (!ghost?.topicId) continue;
      if (!vanished.has(ghost.topicId)) vanished.set(ghost.topicId, []);
      vanished.get(ghost.topicId)!.push(ghost);
    }
    for (const list of vanished.values()) list.sort((a, b) => (a.slot ?? 0) - (b.slot ?? 0));

    const now = performance.now();
    // 一次涌进来很多（首屏、清掉筛选）时按种类错开，像一层层长出来；逐拍回放时几乎同时
    const bulk = previous.size === 0 || fresh.length > 14;
    const indexOfKind = new Map<Kind, number>();
    const domain = nodes.find((node) => node.kind === 'domain');
    const jitter = () => (Math.random() - 0.5) * 6;
    for (const node of fresh) {
      const index = indexOfKind.get(node.kind) ?? 0;
      indexOfKind.set(node.kind, index + 1);
      let delay = 0;
      if (bulk) {
        delay =
          node.kind === 'domain'
            ? 0
            : node.kind === 'topic'
              ? 140 + index * 45
              : node.kind === 'ghost'
                ? 520 + Math.min(index * 10, 500)
                : node.kind === 'document'
                  ? 700 + Math.min(index * 70, 500)
                  : node.kind === 'card'
                    ? 900 + Math.min(index * 7, 700)
                    : 1300 + Math.min(index * 40, 400);
      } else if (node.kind === 'card') {
        delay = Math.min(index * 40, 400);
      }
      bornRef.current.set(node.id, now + delay);

      if (node.kind === 'card') {
        const topicId = graph.cardTopic.get(node.id);
        const slot = topicId ? vanished.get(topicId)?.shift() : undefined;
        if (slot && typeof slot.x === 'number') {
          // 补上缺口：新卡片落在刚空出来的那个虚线槽位上
          filledRef.current.add(node.id);
          node.x = slot.x;
          node.y = slot.y;
          node.vx = 0;
          node.vy = 0;
          continue;
        }
      }
      if (typeof node.x === 'number') continue;
      const parent =
        [...(graph.neighbors.get(node.id) ?? [])]
          .map((id) => graph.byId.get(id))
          .filter((item): item is GraphNode => !!item && ids.has(item.id) && typeof item.x === 'number')
          .sort((a, b) => KIND_ORDER[b.kind] - KIND_ORDER[a.kind])[0] ?? domain;
      node.x = (parent?.x ?? 0) + jitter();
      node.y = (parent?.y ?? 0) + jitter();
      node.vx = 0;
      node.vy = 0;
    }
    prevVisibleRef.current = ids;
    return { nodes, links };
  }, [graph, visibleKey]);

  const cluster = useMemo(() => {
    const map = new Map<string, string | null>();
    for (const node of graph.nodes) map.set(node.id, clusterKey(node, layout, graph));
    return map;
  }, [graph, layout]);

  useEffect(() => {
    // 分组中心按完整图谱固定，回放时不会随着某组长出一张卡片而整圈移位。
    const settled = graph.nodes.filter((node) => node.kind !== 'ghost' ||
      (node.slot ?? 0) >= topicCountAt(graph, node.topicId ?? '', null));
    centersRef.current = clusterCenters(settled, layout, graph);
  }, [layout, graph]);

  useLayoutEffect(() => {
    cameraManualRef.current = false;
    fitPendingRef.current = true;
  }, [layout, pixelRatio]);

  const fitGraph = useCallback((duration = 700) => {
    const fg = graphRef.current;
    const container = containerRef.current;
    const env = envRef.current;
    if (!fg || !container || env.visible.length === 0) return;
    let left = Infinity;
    let right = -Infinity;
    let top = Infinity;
    let bottom = -Infinity;
    for (const node of env.visible) {
      if (typeof node.x !== 'number' || typeof node.y !== 'number') continue;
      const radius = nodeRadius(node) + 12;
      left = Math.min(left, node.x - radius);
      right = Math.max(right, node.x + radius);
      top = Math.min(top, node.y - radius);
      bottom = Math.max(bottom, node.y + radius);
    }
    if (env.layout !== 'network') {
      for (const hull of env.hulls.values()) {
        left = Math.min(left, hull.x - hull.r);
        right = Math.max(right, hull.x + hull.r);
        top = Math.min(top, hull.y - hull.r);
        bottom = Math.max(bottom, hull.y + hull.r);
      }
    }
    if (!Number.isFinite(left)) return;
    const width = container.clientWidth;
    const height = container.clientHeight;
    const side = Math.min(68, width * 0.16);
    const insetTop = width < 850 ? 98 : 70;
    const insetBottom = 44;
    const zoom = clamp(Math.min(
      Math.max(1, width - side * 2) / Math.max(1, right - left),
      Math.max(1, height - insetTop - insetBottom) / Math.max(1, bottom - top),
    ), 0.08, env.visible.length === 1 ? 1.8 : 2.6);
    const ms = env.reduced ? 0 : duration;
    fg.centerAt((left + right) / 2, (top + bottom) / 2 - (insetTop - insetBottom) / (2 * zoom), ms);
    fg.zoom(zoom, ms);
    fitPendingRef.current = false;
  }, []);

  useEffect(() => {
    if (playing || status !== 'ready' || cameraManualRef.current) return;
    fitPendingRef.current = true;
    const timer = window.setTimeout(() => {
      if (fitPendingRef.current && !cameraManualRef.current) fitGraph();
    }, reduced ? 0 : 1200);
    return () => window.clearTimeout(timer);
  }, [playing, layout, visibleData, size, reduced, status, pixelRatio, fitGraph]);

  // 回放：按「拍」匀速推进——每个有新内容的时刻占同样长的时间，
  // 导入集中在某一天、之后隔了几周才进化的情况下也不会长时间停着不动
  useEffect(() => {
    if (!playing || steps.length === 0) return;
    const total = steps.length;
    const start =
      timeCursor === null || timeCursor >= steps[total - 1]
        ? 0
        : steps.filter((step) => step <= timeCursor).length;
    const intro = playMode === 'intro';
    const span = intro ? clamp(total * 60, 2400, 5600) : clamp(total * 140, 3600, 11000);
    const hold = start === 0 ? (intro ? 1500 : 700) : 0;
    const duration = ((total - start) / total) * span;
    if (start === 0) setTimeCursor(steps[0] - 1);
    const startedAt = performance.now();
    let last = start;
    let frame = 0;
    const tick = (now: number) => {
      const elapsed = now - startedAt - hold;
      if (elapsed >= 0) {
        const progress = Math.min(1, elapsed / Math.max(1, duration));
        if (progress >= 1) {
          setTimeCursor(null);
          setPlaying(false);
          return;
        }
        const revealed = start + Math.floor((total - start) * progress);
        if (revealed !== last) {
          last = revealed;
          setTimeCursor(revealed === 0 ? steps[0] - 1 : steps[revealed - 1]);
        }
      }
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
    // 只在开始播放时取一次起点，回放过程中 timeCursor 的变化不应重启动画
  }, [playing, steps]);

  // 回放时镜头跟着图一起长大
  useEffect(() => {
    if (!playing) return;
    const timer = window.setInterval(() => {
      if (!cameraManualRef.current) fitGraph(1000);
    }, 1200);
    return () => window.clearInterval(timer);
  }, [playing, fitGraph]);

  const selected = selectedId ? graph.byId.get(selectedId) ?? null : null;
  const selectedTopic =
    selected?.kind === 'topic' ? selected : selected?.kind === 'ghost' ? graph.byId.get(selected.topicId ?? '') ?? null : null;
  const hovered = hoverId ? graph.byId.get(hoverId) ?? null : null;
  useEffect(() => {
    const visible = new Set(visibleData.nodes.map((node) => node.id));
    if (hoverId && !visible.has(hoverId)) setHoverId(null);
    if (selectedId && !visible.has(selectedId)) {
      const topicId = graph.byId.get(selectedId)?.topicId;
      setSelectedId(topicId && visible.has(topicId) ? topicId : null);
    }
  }, [visibleData, graph, hoverId, selectedId]);
  const focusId = hoverId ?? selectedId;
  const focusSet = useMemo(() => {
    if (!focusId) return null;
    const set = new Set<string>([focusId]);
    graph.neighbors.get(focusId)?.forEach((id) => set.add(id));
    return set;
  }, [focusId, graph]);

  // 每帧画图要用的状态同步进 ref，画图回调本身不变，force-graph 不必反复重设
  useLayoutEffect(() => {
    const env = envRef.current;
    env.palette = palette;
    env.reduced = reduced;
    env.layout = layout;
    env.focusSet = focusSet;
    env.focusId = focusId;
    env.hoverId = hoverId;
    env.selectedId = selectedId;
    env.born = bornRef.current;
    env.filled = filledRef.current;
    env.level = levelRef.current;
    env.topicCount = topicState.count;
    env.cluster = cluster;
    env.visible = visibleData.nodes;
    env.showLabels = showLabels;
    env.clusterColor = (key: string) => {
      if (key.startsWith('topic:')) {
        const empty = (topicState.count.get(key) ?? 0) === 0;
        return { color: empty ? palette.muted : masteryColor(palette, topicState.mastery.get(key)), dashed: empty };
      }
      if (key.startsWith('card:')) return { color: cardKindColor(palette, key.slice(5)), dashed: false };
      if (key === 'docs') return { color: palette.ai, dashed: false };
      if (key === 'insights') return { color: palette.insight, dashed: false };
      return { color: palette.muted, dashed: key === 'topics' || key === 'other' };
    };
    const targets = new Map(topicState.mastery);
    const domain = graph.nodes.find((node) => node.kind === 'domain');
    if (domain && topicState.overall !== null) targets.set(domain.id, topicState.overall);
    targetsRef.current = targets;
  });

  // 减弱动画时画布会在布局结束后停止重绘，文字和悬停变化仍需立即反映。
  useEffect(() => {
    const fg = graphRef.current;
    if (reduced && fg) fg.zoom(fg.zoom(), 0);
  }, [showLabels, hoverId, selectedId, reduced, status]);

  // 布局用的力：「关系」是自然展开，「按主题 / 按类型」再加一股把节点拉向组中心的力
  useEffect(() => {
    const fg = graphRef.current;
    if (!fg) return;
    const clustered = layout !== 'network';
    const charge = fg.d3Force('charge') as
      | { strength?: (fn: (node: GraphNode) => number) => unknown; distanceMax?: (value: number) => unknown }
      | undefined;
    charge?.strength?.((node: GraphNode) => {
      const strength: Record<Kind, number> = clustered
        ? { domain: -120, topic: -90, document: -60, card: -30, insight: -40, ghost: -18 }
        : { domain: -260, topic: -150, document: -110, card: -45, insight: -45, ghost: -25 };
      return strength[node.kind];
    });
    charge?.distanceMax?.(clustered ? 150 : Infinity);
    const link = fg.d3Force('link') as
      | {
          distance?: (fn: (link: GraphLink) => number) => unknown;
          strength?: (fn: (link: GraphLink) => number) => unknown;
        }
      | undefined;
    link?.distance?.((item: GraphLink) =>
      item.kind === 'topic' ? (clustered ? 70 : 130) : item.kind === 'source' ? 72 : item.kind === 'ghost' ? 40 : item.kind === 'insight' ? 48 : 46,
    );
    const degree = (id: string) => Math.max(1, graph.neighbors.get(id)?.size ?? 1);
    link?.strength?.((item: GraphLink) => {
      const a = endId(item.source);
      const b = endId(item.target);
      if (item.kind === 'ghost') return 0.8;
      if (!clustered) return 1 / Math.min(degree(a), degree(b));
      return cluster.get(a) === cluster.get(b) ? 0.5 : 0.01;
    });
    fg.d3Force('collide', collideForce((node) => nodeRadius(node) + (node.kind === 'ghost' ? 6 : 9)));
    const pull = clustered
      ? clusterForce(
          (node) => {
            const key = cluster.get(node.id);
            return key ? centersRef.current.get(key) ?? null : null;
          },
          (node) => (node.kind === 'topic' ? 0.2 : node.kind === 'ghost' ? 0.07 : 0.1),
        )
      : null;
    (fg.d3Force as unknown as (name: string, force: unknown) => void)('cluster', pull);
    fg.d3ReheatSimulation();
  }, [layout, graph, cluster, status, pixelRatio]);

  useEffect(() => {
    fitPendingRef.current = true;
  }, [layout]);

  const onFramePre = useCallback((ctx: CanvasRenderingContext2D, scale: number) => {
    const env = envRef.current;
    const now = performance.now();
    const dt = Math.min(100, now - (lastFrameRef.current || now));
    lastFrameRef.current = now;
    env.now = now;
    // 主题的液面缓缓升到目标掌握度；还没轮到出场的先不动
    const k = env.reduced ? 1 : 1 - Math.exp(-dt / 320);
    for (const [id, target] of targetsRef.current) {
      const born = env.born.get(id);
      if (!env.reduced && born !== undefined && now < born + 250) continue;
      const current = env.level.get(id) ?? 0;
      env.level.set(id, Math.abs(target - current) < 0.002 ? target : current + (target - current) * k);
    }
    paintClusters(ctx, scale, env);
  }, []);
  const onFramePost = useCallback((ctx: CanvasRenderingContext2D, scale: number) => {
    paintLabels(ctx, scale, envRef.current);
  }, []);
  const drawNode = useCallback((node: GraphNode, ctx: CanvasRenderingContext2D, scale: number) => {
    paintNode(node, ctx, scale, envRef.current);
  }, []);
  const drawLink = useCallback((link: GraphLink, ctx: CanvasRenderingContext2D, scale: number) => {
    paintLink(link, ctx, scale, envRef.current);
  }, []);

  const focusNode = useCallback(
    (id: string, zoom = 2.4) => {
      cameraManualRef.current = true;
      fitPendingRef.current = false;
      setPlaying(false);
      setSelectedId(id);
      const node = graph.byId.get(id);
      if (node && hidden.has(node.kind)) setHidden((previous) => {
        const next = new Set(previous);
        next.delete(node.kind);
        return next;
      });
      if (node && typeof node.x === 'number' && typeof node.y === 'number') {
        graphRef.current?.centerAt(node.x, node.y, reduced ? 0 : 600);
        graphRef.current?.zoom(zoom, reduced ? 0 : 600);
      }
    },
    [graph, hidden, reduced],
  );

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setSelectedId(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const toggleKind = (kind: Kind) => {
    setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(kind)) next.delete(kind);
      else next.add(kind);
      return next;
    });
  };

  const startReplay = () => {
    if (playing) {
      setPlaying(false);
      return;
    }
    setPlayMode('replay');
    cameraManualRef.current = false;
    setPlaying(true);
  };

  const skipIntro = () => {
    setPlaying(false);
    setTimeCursor(null);
    cameraManualRef.current = false;
  };

  const generateOutline = async () => {
    if (!spaceId || outlineBusy) return;
    setOutlineBusy(true);
    try {
      await expertiseService.generateOutline(spaceId);
      toast.success('领域大纲已生成，灰色虚线球就是还没掌握的主题');
      setReloadToken((n) => n + 1);
    } catch (err) {
      toast.error((err as Error)?.message || '生成大纲失败');
    } finally {
      setOutlineBusy(false);
    }
  };

  const openSelected = (node: GraphNode) => {
    if (!spaceId) return;
    if (node.kind === 'document' && node.ref_id) {
      openReader({ documentId: node.ref_id, documentTitle: node.label });
    } else if (node.kind === 'card') {
      navigate(`/s/${spaceId}/memory?tab=cards&q=${encodeURIComponent(node.label)}`);
    } else if (node.kind === 'insight') {
      navigate(`/s/${spaceId}/memory?tab=insights&q=${encodeURIComponent(node.label)}`);
    } else if (node.kind === 'topic') {
      navigate(`/s/${spaceId}/expertise`);
    }
  };

  // 悬停提示跟着指针走：直接改样式，不为鼠标移动重渲染整页
  const onPointerMove = (event: React.PointerEvent<HTMLDivElement>) => {
    const tip = tooltipRef.current;
    const box = containerRef.current?.getBoundingClientRect();
    if (!tip || !box) return;
    const x = event.clientX - box.left;
    const y = event.clientY - box.top;
    const flip = x > box.width - 260;
    const flipY = y > box.height - 120;
    tip.style.transform = `translate(${flip ? x - 14 : x + 14}px, ${flipY ? y - 14 : y + 14}px) translate(${flip ? '-100%' : '0'}, ${flipY ? '-100%' : '0'})`;
  };

  const stats = data?.stats;
  const overallShown = useCountUp(topicState.overall === null ? null : topicState.overall * 100, reduced ? 0 : 700);
  const topicsCovered = [...topicState.count.values()].filter((count) => count > 0).length;
  const topicsTotal = topicState.count.size;
  const topicsSolid = [...topicState.count].filter(([id, count]) => isSolid(count, topicState.mastery.get(id) ?? 0)).length;

  const counts = useMemo(() => {
    const result = { document: 0, card: 0, insight: 0, candidate: 0 };
    for (const node of graph.nodes) {
      if (!bornBy(node, timeCursor)) continue;
      if (node.kind === 'document') result.document += 1;
      else if (node.kind === 'card') result.card += 1;
      else if (node.kind === 'insight' && node.status === 'active') result.insight += 1;
      else if (node.kind === 'insight' && node.status === 'candidate') result.candidate += 1;
    }
    return result;
  }, [graph, timeCursor]);

  const topicRows = useMemo(() => {
    const rows = graph.nodes
      .filter((node) => node.kind === 'topic')
      .map((node) => ({
        id: node.id,
        info: graph.topicInfo.get(node.id),
        label: node.label,
        count: topicState.count.get(node.id) ?? 0,
        mastery: topicState.mastery.get(node.id) ?? 0,
      }))
      .filter((row) => topicFilter === 'all' || !isSolid(row.count, row.mastery));
    const groups = new Map<string, typeof rows>();
    for (const row of rows) {
      const key = row.info?.importance ?? 'common';
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key)!.push(row);
    }
    for (const list of groups.values()) list.sort((a, b) => a.count - b.count || a.mastery - b.mastery);
    return [...groups.entries()].sort((a, b) => (IMPORTANCE_ORDER[a[0]] ?? 3) - (IMPORTANCE_ORDER[b[0]] ?? 3));
  }, [graph, topicState, topicFilter]);

  const milestones = [...(data?.milestones ?? [])].reverse();
  const reachedMilestones = milestones.filter((milestone) => timeCursor === null || milestone.at <= timeCursor);
  const hasOutline = topicsTotal > 0;
  const progress = steps.length ? (cursorStep / steps.length) * 100 : 100;

  const neighborGroups = useMemo(() => {
    if (!selected) return [];
    const groups = new Map<Kind, GraphNode[]>();
    for (const id of graph.neighbors.get(selected.id) ?? []) {
      const node = graph.byId.get(id);
      if (!node || !bornBy(node, timeCursor) || node.kind === 'ghost' || node.kind === 'domain') continue;
      if (!groups.has(node.kind)) groups.set(node.kind, []);
      groups.get(node.kind)!.push(node);
    }
    return [...groups.entries()].sort((a, b) => KIND_ORDER[a[0]] - KIND_ORDER[b[0]]);
  }, [selected, graph, timeCursor]);

  const tooltip = hovered ? describe(hovered) : null;
  function describe(node: GraphNode): { kind: string; title: string; meta: string; hint?: string } {
    if (node.kind === 'ghost') {
      const topic = graph.byId.get(node.topicId ?? '');
      return {
        kind: '待补',
        title: node.status === 'subtopic' ? node.label : `「${topic?.label ?? ''}」还差一张卡片`,
        meta: `属于「${topic?.label ?? ''}」`,
        hint: '点击补上',
      };
    }
    if (node.kind === 'topic') {
      const count = topicState.count.get(node.id) ?? 0;
      return {
        kind: '主题',
        title: node.label,
        meta:
          count === 0
            ? '还是空白：没有任何卡片命中'
            : `掌握度 ${percent(topicState.mastery.get(node.id))}% · ${count} 张卡片`,
        hint: count < CARD_SATURATION ? '点击补上缺口' : undefined,
      };
    }
    if (node.kind === 'card') {
      return {
        kind: CARD_KIND_LABEL[node.status ?? ''] ?? '卡片',
        title: node.label,
        meta: `置信度 ${percent(node.mastery)}%${filledRef.current.has(node.id) ? ' · 补上的缺口' : ''}`,
      };
    }
    if (node.kind === 'document') {
      const produced = [...(graph.neighbors.get(node.id) ?? [])].filter((id) => id.startsWith('card:')).length;
      return {
        kind: '资料',
        title: node.label,
        meta: node.status === 'failed' ? '处理失败' : `抽出 ${produced} 张卡片`,
      };
    }
    if (node.kind === 'insight') {
      const label = node.status === 'candidate' ? '候选' : node.status === 'conflicted' ? '冲突' : '生效';
      return { kind: '经验', title: node.label, meta: `${label} · 置信度 ${percent(node.mastery)}%` };
    }
    return { kind: '领域', title: node.label, meta: `整体掌握度 ${percent(topicState.overall)}%` };
  }

  return (
    <div className="knowledge-map flex h-full w-full flex-col overflow-hidden">
      {/* 页头 */}
      <div className="flex shrink-0 flex-wrap items-end justify-between gap-3 px-4 pb-3 pt-5 sm:px-6">
        <div className="min-w-0">
          <div className="mb-1.5 flex items-center gap-1.5 text-[11.5px] font-medium text-muted-foreground">
            <Network className="h-3.5 w-3.5 text-primary" />
            知识网络
          </div>
          <h1 className="font-display text-[26px] leading-tight text-foreground">知识图谱</h1>
          <p className="mt-1 text-[13px] text-muted-foreground">
            它懂了哪些、还缺哪些，以及这些知识是怎么一步步长出来的。
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <div className="flex items-center gap-0.5 rounded-full border border-border bg-muted/40 p-0.5">
            {KIND_FILTERS.map(({ kind, label, icon: Icon }) => {
              const active = !hidden.has(kind);
              return (
                <button
                  key={kind}
                  type="button"
                  aria-pressed={active}
                  onClick={() => toggleKind(kind)}
                  title={active ? `隐藏${label}` : `显示${label}`}
                  className={cn(
                    'flex h-7 items-center gap-1.5 rounded-full px-2.5 text-[12px] transition-all duration-200',
                    active
                      ? 'bg-card text-foreground shadow-[var(--shadow-card)]'
                      : 'text-muted-foreground/70 line-through decoration-muted-foreground/40 hover:text-foreground',
                  )}
                >
                  <Icon className={cn('h-3.5 w-3.5', active && kind === 'ghost' && 'text-muted-foreground')} />
                  {label}
                </button>
              );
            })}
          </div>
          <Button
            variant="ghost"
            size="sm"
            className="h-8 gap-1.5 text-xs"
            onClick={() => setReloadToken((n) => n + 1)}
            disabled={refreshing}
          >
            <RefreshCw className={cn('h-3.5 w-3.5', refreshing && 'animate-spin')} />
            刷新
          </Button>
        </div>
      </div>

      <FourStateView
        status={status === 'loading' ? 'loading' : status === 'error' ? 'error' : status === 'empty' ? 'empty' : 'ready'}
        error={error}
        onRetry={() => setReloadToken((n) => n + 1)}
        emptyTitle="知识网络还是空的"
        emptyDescription="导入资料并抽取知识卡片后，这里会画出主题、资料、卡片与经验之间的关系。"
        emptyActionLabel="去导入资料"
        emptyIcon={<Network className="h-8 w-8 text-primary" />}
        onEmptyAction={() => spaceId && navigate(`/s/${spaceId}/library?import=1`)}
      >
        <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-3 pb-3 sm:px-6 sm:pb-5 lg:flex-row lg:overflow-hidden">
          {/* 图 + 时间轴 */}
          <div className="relative flex min-h-[460px] min-w-0 flex-1 flex-col overflow-hidden rounded-2xl border border-border bg-card shadow-[var(--shadow-card)]">
            <div
              ref={containerRef}
              className={cn('graph-canvas @container relative min-h-0 flex-1', hovered && 'cursor-pointer')}
              aria-label="知识网络图"
              onPointerMove={onPointerMove}
              onPointerLeave={() => setHoverId(null)}
              onPointerDownCapture={(event) => {
                if (event.target instanceof HTMLCanvasElement) {
                  cameraManualRef.current = true;
                  fitPendingRef.current = false;
                }
              }}
              onWheelCapture={() => {
                cameraManualRef.current = true;
                fitPendingRef.current = false;
              }}
            >
              {status === 'ready' && (
                <ForceGraph2D<GraphNode, GraphLink>
                  key={pixelRatio}
                  ref={graphRef}
                  width={size.width}
                  height={size.height}
                  graphData={visibleData}
                  backgroundColor="rgba(0,0,0,0)"
                  nodeId="id"
                  nodeRelSize={4}
                  nodeVal={(node) => nodeRadius(node) ** 2 / 16}
                  nodeCanvasObject={drawNode}
                  nodePointerAreaPaint={(node, color, ctx) => paintPointerArea(node, color, ctx, envRef.current)}
                  linkCanvasObjectMode={() => 'replace'}
                  linkCanvasObject={drawLink}
                  onRenderFramePre={onFramePre}
                  onRenderFramePost={onFramePost}
                  autoPauseRedraw={reduced}
                  minZoom={0.08}
                  maxZoom={6}
                  d3AlphaDecay={0.026}
                  d3VelocityDecay={0.34}
                  warmupTicks={0}
                  cooldownTicks={260}
                  onEngineStop={() => {
                    if (!cameraManualRef.current && !playing) fitGraph();
                  }}
                  onNodeClick={(node) => {
                    setPlaying(false);
                    setSelectedId(node.id === selectedId ? null : node.id);
                  }}
                  onNodeHover={(node) => setHoverId(node ? node.id : null)}
                  onBackgroundClick={() => setSelectedId(null)}
                  enableNodeDrag
                />
              )}

              {/* 图例 */}
              <div className="pointer-events-none absolute left-3 top-[54px] flex max-w-[calc(100%-24px)] flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-border/70 bg-card/90 px-2.5 py-1.5 text-[11px] text-muted-foreground backdrop-blur @[850px]:top-3 @[850px]:max-w-[calc(100%-480px)]">
                <span className="flex items-center gap-1">
                  <span className="relative h-2.5 w-2.5 overflow-hidden rounded-full border border-accent-insight">
                    <span className="absolute inset-x-0 bottom-0 h-full bg-accent-insight" />
                  </span>
                  扎实
                </span>
                <span className="flex items-center gap-1">
                  <span className="relative h-2.5 w-2.5 overflow-hidden rounded-full border border-accent-ai">
                    <span className="absolute inset-x-0 bottom-0 h-1/2 bg-accent-ai" />
                  </span>
                  部分
                </span>
                <span className="flex items-center gap-1">
                  <span className="relative h-2.5 w-2.5 overflow-hidden rounded-full border border-accent-warn">
                    <span className="absolute inset-x-0 bottom-0 h-1/4 bg-accent-warn" />
                  </span>
                  薄弱
                </span>
                <span className="flex items-center gap-1">
                  <span className="h-2.5 w-2.5 rounded-full border border-dashed border-muted-foreground bg-muted/60" />
                  待补
                </span>
                <span className="hidden h-3 w-px bg-border sm:block" />
                <span className="hidden items-center gap-1 sm:flex">
                  <span className="h-2 w-2 rounded-[2px] bg-accent-ai" /> 资料
                </span>
                <span className="hidden items-center gap-1 sm:flex">
                  <span className="h-1.5 w-1.5 rounded-full bg-muted-foreground/70" /> 卡片
                </span>
                <span className="hidden items-center gap-1 sm:flex">
                  <span className="h-2 w-2 rotate-45 bg-accent-insight" /> 经验
                </span>
              </div>

              {/* 布局与缩放 */}
              <div className="absolute right-3 top-3 flex items-center gap-1.5">
                <label
                  htmlFor="graph-persistent-labels"
                  className="flex h-8 cursor-pointer items-center gap-2 rounded-lg border border-border/70 bg-card/90 px-2.5 text-[11.5px] text-muted-foreground backdrop-blur"
                  title="开启后持续显示节点与分类文字；关闭时仅在悬停时显示"
                >
                  常驻文字
                  <Switch
                    id="graph-persistent-labels"
                    size="sm"
                    checked={showLabels}
                    onCheckedChange={setShowLabels}
                    aria-label="常驻显示文字"
                  />
                </label>
                <div
                  role="radiogroup"
                  aria-label="布局"
                  className="flex items-center gap-0.5 rounded-lg border border-border/70 bg-card/90 p-0.5 backdrop-blur"
                >
                  {LAYOUTS.map(({ mode, label, icon: Icon, hint }) => (
                    <button
                      key={mode}
                      type="button"
                      role="radio"
                      aria-label={label}
                      aria-checked={layout === mode}
                      title={hint}
                      onClick={() => setLayout(mode)}
                      className={cn(
                        'flex h-7 items-center gap-1 rounded-md px-2 text-[11.5px] transition-colors',
                        layout === mode ? 'bg-foreground text-background' : 'text-muted-foreground hover:text-foreground',
                      )}
                    >
                      <Icon className="h-3.5 w-3.5" />
                      <span className="hidden @[600px]:inline">{label}</span>
                    </button>
                  ))}
                </div>
                <div className="flex items-center rounded-lg border border-border/70 bg-card/90 backdrop-blur">
                  {[
                    { label: '放大', icon: ZoomIn, run: () => {
                      cameraManualRef.current = true;
                      graphRef.current?.zoom(Math.min(6, (graphRef.current?.zoom() ?? 1) * 1.35), reduced ? 0 : 300);
                    } },
                    { label: '缩小', icon: ZoomOut, run: () => {
                      cameraManualRef.current = true;
                      graphRef.current?.zoom(Math.max(0.08, (graphRef.current?.zoom() ?? 1) / 1.35), reduced ? 0 : 300);
                    } },
                    { label: '适应画布', icon: Maximize2, run: () => {
                      cameraManualRef.current = false;
                      fitGraph(500);
                    } },
                  ].map(({ label, icon: Icon, run }) => (
                    <button
                      key={label}
                      type="button"
                      onClick={run}
                      aria-label={label}
                      title={label}
                      className="flex h-8 w-8 items-center justify-center text-muted-foreground transition-colors hover:text-foreground"
                    >
                      <Icon className="h-3.5 w-3.5" />
                    </button>
                  ))}
                </div>
              </div>

              {/* 悬停提示 */}
              <div
                ref={tooltipRef}
                className="pointer-events-none absolute left-0 top-0 z-10 will-change-transform"
                aria-hidden
              >
                {tooltip && (
                  <div
                    key={hoverId}
                    className="max-w-[240px] rounded-lg border border-border bg-popover/95 px-2.5 py-2 text-popover-foreground shadow-lg backdrop-blur animate-in fade-in-0 zoom-in-95 duration-150"
                  >
                    <div className="text-[10.5px] font-medium text-muted-foreground">{tooltip.kind}</div>
                    <div className="mt-0.5 text-[12.5px] font-medium leading-snug">{tooltip.title}</div>
                    <div className="mt-0.5 text-[11px] text-muted-foreground">{tooltip.meta}</div>
                    {tooltip.hint && <div className="mt-1 text-[11px] font-medium text-primary">{tooltip.hint}</div>}
                  </div>
                )}
              </div>

              {/* 开场生长时可以跳过 */}
              {playing && playMode === 'intro' && (
                <button
                  type="button"
                  onClick={skipIntro}
                  className="absolute bottom-3 left-1/2 flex -translate-x-1/2 items-center gap-1.5 rounded-full border border-border/70 bg-card/90 px-3 py-1.5 text-[11.5px] text-muted-foreground shadow-sm backdrop-blur transition-colors animate-in fade-in-0 slide-in-from-bottom-2 hover:text-foreground"
                >
                  <span className="relative flex h-2 w-2">
                    <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-primary opacity-60" />
                    <span className="relative inline-flex h-2 w-2 rounded-full bg-primary" />
                  </span>
                  {atStart ? '先画出该懂的主题…' : '知识正在长出来…'}
                  <SkipForward className="h-3 w-3" />
                  跳过
                </button>
              )}

              {/* 没有大纲：缺口无从谈起，引导去生成 */}
              {!hasOutline && !playing && (
                <div className="absolute bottom-3 left-3 max-w-[300px] rounded-xl border border-dashed border-border bg-card/95 p-3 shadow-sm backdrop-blur animate-in fade-in-0 slide-in-from-bottom-2">
                  <div className="flex items-center gap-1.5 text-[12.5px] font-medium text-foreground">
                    <CircleDashed className="h-3.5 w-3.5 text-muted-foreground" />
                    还不知道「该懂什么」
                  </div>
                  <p className="mt-1 text-[11.5px] leading-relaxed text-muted-foreground">
                    生成领域大纲后，没掌握的主题会以灰色虚线球出现在图上，可以一个个补上。
                  </p>
                  <Button size="sm" className="mt-2 h-7 gap-1 text-[11.5px]" onClick={generateOutline} disabled={outlineBusy}>
                    {outlineBusy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Wand2 className="h-3 w-3" />}
                    {outlineBusy ? '正在生成大纲…' : '生成领域大纲'}
                  </Button>
                </div>
              )}
            </div>

            {/* 成长时间轴 */}
            {timeRange && (
              <div className="shrink-0 border-t border-border/70 px-3 py-2.5 sm:px-4">
                <div className="flex items-center gap-3">
                  <Button
                    size="icon"
                    variant={playing ? 'default' : 'outline'}
                    className="h-8 w-8 shrink-0 rounded-full transition-colors"
                    onClick={startReplay}
                    aria-label={playing ? '暂停回放' : '回放成长过程'}
                    title={playing ? '暂停回放' : '从零回放成长过程'}
                  >
                    {playing ? <Pause className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5 translate-x-px" />}
                  </Button>
                  <div className="relative min-w-0 flex-1">
                    {/* 里程碑刻度：点一下跳到那一刻 */}
                    <div className="absolute inset-x-0 -top-1.5 h-2.5">
                      {(data?.milestones ?? []).map((milestone, index) => {
                        const reached = timeCursor === null || milestone.at <= timeCursor;
                        return (
                          <button
                            key={`${milestone.at}-${index}`}
                            type="button"
                            title={`${formatDate(milestone.at)} · ${milestone.label}`}
                            aria-label={`跳到 ${formatDate(milestone.at)}：${milestone.label}`}
                            onClick={() => {
                              setPlaying(false);
                              setTimeCursor(milestone.at >= timeRange.max ? null : milestone.at);
                            }}
                            className={cn('absolute -top-1 flex h-4 w-4 -translate-x-1/2 justify-center py-1 transition-opacity hover:opacity-100', !reached && 'opacity-30')}
                            style={{
                              left: `${(stepIndexAt(steps, milestone.at) / steps.length) * 100}%`,
                            }}
                          >
                            <span className={cn('h-2.5 w-[3px] rounded-full',
                              milestone.kind === 'evolution' ? 'bg-primary' : milestone.kind === 'insight' ? 'bg-accent-insight' : 'bg-muted-foreground/45',
                            )} />
                          </button>
                        );
                      })}
                    </div>
                    <input
                      type="range"
                      aria-label="时间轴：拖动查看当时的知识网络"
                      min={0}
                      max={steps.length}
                      step={1}
                      value={cursorStep}
                      aria-valuetext={timeCursor === null ? '现在' : atStart ? '起点，尚未积累知识' : `截至 ${formatDate(cursorValue)}，已有 ${counts.card} 张卡片`}
                      onChange={(event) => {
                        setPlaying(false);
                        cameraManualRef.current = false;
                        setTimeCursor(cursorAtStep(steps, Number(event.target.value)));
                      }}
                      className="growth-range mt-1.5 w-full"
                      style={{ '--progress': `${progress}%` } as React.CSSProperties}
                    />
                  </div>
                </div>
                <div className="mt-1 flex flex-wrap items-center justify-between gap-x-3 gap-y-0.5 pl-11 text-[11.5px] text-muted-foreground">
                  <span className="font-mono">
                    {timeCursor === null ? '现在' : atStart ? '起点' : `截至 ${formatDate(cursorValue)}`}
                  </span>
                  <span className="tabular-nums">
                    {counts.document} 份资料 · {counts.card} 张卡片 · {counts.insight} 条生效经验
                    {hasOutline && ` · ${topicsCovered}/${topicsTotal} 主题有覆盖`}
                  </span>
                </div>
              </div>
            )}
          </div>

          {/* 右侧：选中节点 / 掌握概览 / 主题 / 里程碑 */}
          <aside
            aria-label="掌握程度与成长脉络"
            className={cn('flex shrink-0 flex-col gap-3 lg:overflow-y-auto', isWide ? 'w-[340px]' : 'w-full')}
          >
            {selected && (
              <section
                key={selected.id}
                className="surface-card p-4 animate-in fade-in-0 slide-in-from-right-2 duration-200"
              >
                <div className="flex items-start justify-between gap-2">
                  <span
                    className={cn(
                      'rounded-full px-2 py-0.5 text-[11px] font-medium',
                      selected.kind === 'ghost' ||
                        (selected.kind === 'topic' && (topicState.count.get(selected.id) ?? 0) === 0)
                        ? 'border border-dashed border-muted-foreground/50 text-muted-foreground'
                        : 'bg-muted text-muted-foreground',
                    )}
                  >
                    {selected.kind === 'card'
                      ? `${KIND_LABEL.card} · ${CARD_KIND_LABEL[selected.status ?? ''] ?? '卡片'}`
                      : KIND_LABEL[selected.kind]}
                    {selected.kind === 'topic' && (topicState.count.get(selected.id) ?? 0) === 0 ? ' · 缺口' : ''}
                    {selected.kind === 'insight' && selected.status === 'candidate' ? ' · 候选' : ''}
                  </span>
                  <button
                    type="button"
                    onClick={() => setSelectedId(null)}
                    aria-label="取消选中"
                    className="rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                </div>
                <h2 className="mt-2 text-[14.5px] font-semibold leading-snug text-foreground">
                  {selected.kind === 'ghost' ? selectedTopic?.label : selected.label}
                </h2>
                {selected.kind !== 'ghost' && selected.detail && (
                  <p className="mt-1.5 line-clamp-4 text-[12.5px] leading-relaxed text-muted-foreground">
                    {selected.detail}
                  </p>
                )}
                {selected.kind !== 'domain' && selected.kind !== 'ghost' && typeof selected.mastery === 'number' && (
                  <div className="mt-3">
                    <div className="mb-1 flex justify-between text-[11.5px] text-muted-foreground">
                      <span>{selected.kind === 'topic' ? '掌握度' : '置信度'}</span>
                      <span className="font-mono text-foreground">
                        {percent(selected.kind === 'topic' ? topicState.mastery.get(selected.id) : selected.mastery)}%
                      </span>
                    </div>
                    <div className="h-1.5 overflow-hidden rounded-full bg-muted">
                      <div
                        className="h-full rounded-full transition-[width] duration-700 ease-out"
                        style={{
                          width: `${Math.round(((selected.kind === 'topic' ? topicState.mastery.get(selected.id) : selected.mastery) ?? 0) * 100)}%`,
                          background: masteryColor(
                            palette,
                            selected.kind === 'topic' ? topicState.mastery.get(selected.id) : selected.mastery,
                          ),
                        }}
                      />
                    </div>
                  </div>
                )}

                {selectedTopic && spaceId && graph.topicInfo.get(selectedTopic.id) && (
                  <GapFillPanel
                    spaceId={spaceId}
                    topic={graph.topicInfo.get(selectedTopic.id)!}
                    count={topicState.count.get(selectedTopic.id) ?? 0}
                    mastery={topicState.mastery.get(selectedTopic.id) ?? 0}
                    suggestion={
                      selected.kind === 'ghost' && selected.status === 'subtopic' ? selected.label : null
                    }
                    defaultOpen={selected.kind === 'ghost' || (topicState.count.get(selectedTopic.id) ?? 0) === 0}
                    onFilled={() => {
                      setPlaying(false);
                      setTimeCursor(null);
                      setSelectedId((current) => current === selected.id ? selectedTopic.id : current);
                      setReloadToken((n) => n + 1);
                    }}
                  />
                )}

                {neighborGroups.length > 0 && selected.kind !== 'ghost' && (
                  <div className="mt-3 space-y-2">
                    {neighborGroups.map(([kind, items]) => (
                      <div key={kind}>
                        <div className="mb-1 text-[11px] text-muted-foreground">
                          {kind === 'topic' ? '归属主题' : kind === 'document' ? '来源资料' : `相关${KIND_LABEL[kind]}`} ·{' '}
                          {items.length}
                        </div>
                        <div className="flex flex-wrap gap-1">
                          {items.slice(0, 8).map((item) => (
                            <button
                              key={item.id}
                              type="button"
                              onClick={() => focusNode(item.id)}
                              onMouseEnter={() => setHoverId(item.id)}
                              onMouseLeave={() => setHoverId(null)}
                              className="max-w-full truncate rounded-md border border-border/70 px-1.5 py-0.5 text-[11px] text-foreground transition-colors hover:border-primary/40 hover:bg-muted"
                            >
                              {item.label}
                            </button>
                          ))}
                          {items.length > 8 && (
                            <span className="px-1 py-0.5 text-[11px] text-muted-foreground">+{items.length - 8}</span>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                )}

                <div className="mt-3 flex items-center justify-between text-[11.5px] text-muted-foreground">
                  <span>
                    {typeof selected.created_at === 'number' && selected.kind !== 'topic'
                      ? `加入于 ${formatDate(selected.created_at)}`
                      : ''}
                  </span>
                  {selected.kind !== 'domain' && selected.kind !== 'ghost' && (
                    <button
                      type="button"
                      onClick={() => openSelected(selected)}
                      className="flex items-center gap-0.5 font-medium text-primary hover:underline"
                    >
                      {selected.kind === 'document'
                        ? '打开原文'
                        : selected.kind === 'topic'
                          ? '去专家评测'
                          : '查看详情'}
                      <ArrowUpRight className="h-3 w-3" />
                    </button>
                  )}
                </div>
              </section>
            )}

            <section className="surface-card p-4">
              <div className="flex items-end justify-between gap-3">
                <div>
                  <div className="text-[11.5px] font-medium text-muted-foreground">
                    领域掌握度{timeCursor !== null && <span className="ml-1 text-primary">· 回放中</span>}
                  </div>
                  <div className="mt-1 flex items-baseline gap-1">
                    <span className="font-display text-[38px] leading-none tabular-nums text-foreground">
                      {overallShown === null ? '—' : Math.round(overallShown)}
                    </span>
                    {overallShown !== null && <span className="text-[13px] text-muted-foreground">/ 100</span>}
                  </div>
                </div>
                {hasOutline && (
                  <div className="text-right text-[12px] text-muted-foreground">
                    覆盖 <span className="font-mono text-foreground">{topicsCovered}</span>
                    {' / '}
                    <span className="font-mono">{topicsTotal}</span> 个主题
                  </div>
                )}
              </div>
              {hasOutline ? (
                <>
                  {/* 主题构成：扎实 / 偏薄 / 空白 */}
                  <div className="mt-3 flex h-2 overflow-hidden rounded-full bg-muted">
                    <div
                      className="h-full bg-accent-insight transition-[width] duration-700 ease-out"
                      style={{ width: `${(topicsSolid / topicsTotal) * 100}%` }}
                    />
                    <div
                      className="h-full bg-accent-ai/70 transition-[width] duration-700 ease-out"
                      style={{ width: `${((topicsCovered - topicsSolid) / topicsTotal) * 100}%` }}
                    />
                  </div>
                  <div className="mt-1.5 flex gap-3 text-[11px] text-muted-foreground">
                    <span className="flex items-center gap-1">
                      <span className="h-1.5 w-1.5 rounded-full bg-accent-insight" />
                      扎实 {topicsSolid}
                    </span>
                    <span className="flex items-center gap-1">
                      <span className="h-1.5 w-1.5 rounded-full bg-accent-ai/70" />
                      偏薄 {topicsCovered - topicsSolid}
                    </span>
                    <span className="flex items-center gap-1">
                      <span className="h-1.5 w-1.5 rounded-full border border-dashed border-muted-foreground" />
                      空白 {topicsTotal - topicsCovered}
                    </span>
                  </div>
                </>
              ) : (
                <p className="mt-2 text-[12px] leading-relaxed text-muted-foreground">
                  还没有领域大纲，无法按主题计算掌握度。
                </p>
              )}
              <div className="mt-3 grid grid-cols-4 gap-1.5 text-center">
                {[
                  { label: '资料', value: timeCursor === null ? stats?.documents : counts.document },
                  { label: '卡片', value: timeCursor === null ? stats?.cards : counts.card },
                  { label: '经验', value: timeCursor === null ? stats?.insights_active : counts.insight },
                  { label: '候选', value: timeCursor === null ? stats?.insights_candidate : counts.candidate },
                ].map((item) => (
                  <div key={item.label} className="rounded-lg bg-muted/70 px-1 py-1.5">
                    <div className="font-mono text-[14px] font-semibold tabular-nums text-foreground">{item.value ?? 0}</div>
                    <div className="text-[11px] text-muted-foreground">{item.label}</div>
                  </div>
                ))}
              </div>
              {data?.truncated && (
                <p className="mt-2 text-[11px] text-muted-foreground">
                  卡片较多，图上只画了置信度最高的一部分；掌握度按全部卡片计算。
                </p>
              )}
            </section>

            {hasOutline && (
              <section className="surface-card p-2">
                <div className="flex items-center justify-between px-2 pb-1 pt-1.5">
                  <h2 className="text-[12.5px] font-semibold text-foreground">主题掌握</h2>
                  <div className="flex items-center gap-0.5 rounded-md bg-muted/70 p-0.5 text-[11px]">
                    {(['all', 'gaps'] as const).map((value) => (
                      <button
                        key={value}
                        type="button"
                        onClick={() => setTopicFilter(value)}
                        className={cn(
                          'rounded px-1.5 py-0.5 transition-colors',
                          topicFilter === value ? 'bg-card text-foreground shadow-xs' : 'text-muted-foreground',
                        )}
                      >
                        {value === 'all' ? '全部' : `待补 ${topicsTotal - topicsSolid}`}
                      </button>
                    ))}
                  </div>
                </div>
                {topicRows.length === 0 && (
                  <p className="px-2 py-4 text-center text-[12px] text-muted-foreground">每个主题都补齐了</p>
                )}
                {topicRows.map(([importance, rows]) => (
                  <div key={importance} className="mt-1">
                    <div className="px-2 pb-0.5 pt-1.5 text-[10.5px] font-medium tracking-wide text-muted-foreground">
                      {IMPORTANCE_LABEL[importance] ?? importance}主题 · {rows.length}
                    </div>
                    <ul className="list-card-in space-y-0.5">
                      {rows.map((row) => {
                        const empty = row.count === 0;
                        const thin = !isSolid(row.count, row.mastery);
                        return (
                          <li key={row.id}>
                            <button
                              type="button"
                              aria-label={`${row.label}，${empty ? '待补知识' : `掌握度 ${percent(row.mastery)}%`}`}
                              title={`${row.label} · ${row.count} 张卡片`}
                              onClick={() => focusNode(row.id)}
                              onMouseEnter={() => setHoverId(row.id)}
                              onMouseLeave={() => setHoverId(null)}
                              className={cn(
                                'group w-full rounded-lg px-2 py-2 text-left transition-colors hover:bg-muted/70',
                                (row.id === selectedId || selected?.topicId === row.id) && 'bg-muted',
                              )}
                            >
                              <div className="flex items-center gap-2">
                                <span
                                  className={cn(
                                    'min-w-0 flex-1 truncate text-[12.5px]',
                                    empty ? 'text-muted-foreground' : 'text-foreground',
                                  )}
                                >
                                  {row.label}
                                </span>
                                {thin && (
                                  <span className="hidden shrink-0 text-[10.5px] font-medium text-primary group-hover:inline">
                                    补上
                                  </span>
                                )}
                                <SlotDots filled={Math.min(row.count, CARD_SATURATION)} className="shrink-0" />
                                <span
                                  className={cn(
                                    'w-8 shrink-0 text-right font-mono text-[11px] tabular-nums',
                                    empty ? 'text-muted-foreground/70' : 'text-muted-foreground',
                                  )}
                                >
                                  {empty ? '—' : `${percent(row.mastery)}%`}
                                </span>
                              </div>
                              <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-muted">
                                <div
                                  className="h-full rounded-full transition-[width] duration-700 ease-out"
                                  style={{
                                    width: `${Math.max(empty ? 0 : 4, Math.round(row.mastery * 100))}%`,
                                    background: masteryColor(palette, row.mastery),
                                  }}
                                />
                              </div>
                            </button>
                          </li>
                        );
                      })}
                    </ul>
                  </div>
                ))}
              </section>
            )}

            {milestones.length > 0 && (
              <section className="surface-card p-2">
                <h2 className="flex items-center justify-between px-2 pb-1 pt-1.5 text-[12.5px] font-semibold text-foreground">
                  成长脉络
                  <span className="font-mono text-[11px] font-normal text-muted-foreground">
                    {reachedMilestones.length} / {milestones.length}
                  </span>
                </h2>
                <ol className="relative ml-4 border-l border-border pb-1">
                  {milestones.slice(0, 40).map((milestone, index) => {
                    const Icon = MILESTONE_ICON[milestone.kind];
                    const reached = timeCursor === null || milestone.at <= timeCursor;
                    return (
                      <li key={`${milestone.at}-${index}`}>
                        <button
                          type="button"
                          title="在时间轴上跳到这一刻"
                          onClick={() => {
                            setPlaying(false);
                            setTimeCursor(timeRange && milestone.at >= timeRange.max ? null : milestone.at);
                          }}
                          className={cn(
                            'relative w-full rounded-r-lg py-1.5 pl-4 pr-2 text-left transition-[opacity,background-color] duration-300 hover:bg-muted/60',
                            !reached && 'opacity-35',
                          )}
                        >
                          <span
                            className={cn(
                              'absolute -left-[9px] top-2 flex h-[17px] w-[17px] items-center justify-center rounded-full border bg-card transition-colors',
                              milestone.kind === 'evolution'
                                ? 'border-primary/40 text-primary'
                                : milestone.kind === 'insight'
                                  ? 'border-accent-insight/40 text-accent-insight'
                                  : 'border-border text-muted-foreground',
                            )}
                          >
                            <Icon className="h-2.5 w-2.5" />
                          </span>
                          <div className="text-[12px] leading-snug text-foreground">{milestone.label}</div>
                          <div className="mt-0.5 flex gap-2 text-[11px] text-muted-foreground">
                            <span className="font-mono">{formatDate(milestone.at)}</span>
                            {typeof milestone.value === 'number' && (
                              <span>
                                {milestone.kind === 'insight'
                                  ? `置信度 ${Math.round(milestone.value * 100)}%`
                                  : `专家度 ${milestone.value.toFixed(1)}`}
                              </span>
                            )}
                          </div>
                        </button>
                      </li>
                    );
                  })}
                </ol>
              </section>
            )}
          </aside>
        </div>
      </FourStateView>
    </div>
  );
}
