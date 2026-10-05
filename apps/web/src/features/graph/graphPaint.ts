import {
  CLUSTER_LABEL,
  hashUnit,
  labelPriority,
  nodeRadius,
  type GraphLink,
  type GraphNode,
  type LayoutMode,
} from './graphModel';

export interface Palette {
  primary: string;
  insight: string;
  ai: string;
  warn: string;
  danger: string;
  fg: string;
  muted: string;
  card: string;
  link: string;
  linkStrong: string;
}

/** 画布不认 CSS 变量，换主题时从计算样式里取一次真实颜色。 */
export function readPalette(element: HTMLElement | null, dark: boolean): Palette {
  const style = getComputedStyle(element ?? document.documentElement);
  const read = (name: string, fallback: string) => style.getPropertyValue(name).trim() || fallback;
  return {
    primary: read('--primary', '#c0643f'),
    insight: read('--accent-insight', '#2f8f5b'),
    ai: read('--accent-ai', '#2f6f9f'),
    warn: read('--accent-warn', '#b07a1a'),
    danger: read('--destructive', '#c03030'),
    fg: read('--foreground', '#222'),
    muted: read('--muted-foreground', '#777'),
    card: read('--card', '#fff'),
    link: dark ? 'rgba(250,240,225,0.11)' : 'rgba(70,55,40,0.13)',
    linkStrong: dark ? 'rgba(250,240,225,0.5)' : 'rgba(70,55,40,0.48)',
  };
}

/** 掌握度 → 颜色：低是琥珀（薄弱）、中是蓝、高是绿；没有数值是灰。 */
export function masteryColor(palette: Palette, mastery?: number | null): string {
  if (typeof mastery !== 'number') return palette.muted;
  if (mastery >= 0.75) return palette.insight;
  if (mastery >= 0.4) return palette.ai;
  return palette.warn;
}

/** 卡片类型的颜色，与「知识记忆」卡片左侧色条一致 */
export function cardKindColor(palette: Palette, kind?: string | null): string {
  switch (kind) {
    case 'concept':
      return palette.primary;
    case 'procedure':
      return palette.ai;
    case 'pitfall':
      return palette.warn;
    case 'tool':
      return palette.insight;
    default:
      return palette.muted;
  }
}

/** 每一帧画图需要的全部状态。放在 ref 里，画图回调本身保持稳定。 */
export interface PaintEnv {
  palette: Palette;
  now: number;
  reduced: boolean;
  layout: LayoutMode;
  focusSet: Set<string> | null;
  focusId: string | null;
  hoverId: string | null;
  selectedId: string | null;
  /** 节点出生时刻（performance.now 时钟） */
  born: Map<string, number>;
  /** 落在待补槽位上、把缺口补上的卡片 */
  filled: Set<string>;
  /** 主题 / 领域当前显示的掌握度（逐帧缓动到目标值） */
  level: Map<string, number>;
  /** 主题当前命中的卡片数 */
  topicCount: Map<string, number>;
  /** 节点 → 所在分组 */
  cluster: Map<string, string | null>;
  clusterColor: (key: string) => { color: string; dashed: boolean };
  visible: GraphNode[];
  /** 默认只显示悬停节点的文字，开启后常驻显示。 */
  showLabels: boolean;
  /** 平滑后的分组外框，避免随节点抖动 */
  hulls: Map<string, { x: number; y: number; r: number }>;
}

const APPEAR_MS = 560;
const RIPPLE_MS = 1100;
const LINK_GROW_MS = 700;

const FONT = '"Geist Variable", "PingFang SC", "Microsoft YaHei", sans-serif';

const clamp01 = (value: number) => Math.max(0, Math.min(1, value));
const easeOutCubic = (t: number) => 1 - (1 - t) ** 3;
function easeOutBack(t: number): number {
  const c1 = 1.5;
  const c3 = c1 + 1;
  return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2;
}

/** 出现动画的进度 0~1；还没轮到的节点是负数 */
export function appearProgress(env: PaintEnv, id: string): number {
  if (env.reduced) return 1;
  const born = env.born.get(id);
  if (born === undefined) return 1;
  return (env.now - born) / APPEAR_MS;
}

function dashedCircle(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  r: number,
  scale: number,
  env: PaintEnv,
  phase: number,
) {
  ctx.beginPath();
  ctx.arc(x, y, r, 0, 2 * Math.PI);
  ctx.setLineDash([2.4 / scale, 2 / scale]);
  // 虚线缓慢流动：「这里还空着，等着被补上」
  ctx.lineDashOffset = env.reduced ? 0 : -((env.now / 90 + phase * 20) % 40) / scale;
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.lineDashOffset = 0;
}

function plusMark(ctx: CanvasRenderingContext2D, x: number, y: number, size: number, scale: number) {
  ctx.beginPath();
  ctx.moveTo(x - size, y);
  ctx.lineTo(x + size, y);
  ctx.moveTo(x, y - size);
  ctx.lineTo(x, y + size);
  ctx.lineWidth = 1.2 / scale;
  ctx.lineCap = 'round';
  ctx.stroke();
}

/** 液面：按掌握度从底部灌满，顶上一道轻微的波 */
function liquidFill(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  r: number,
  level: number,
  color: string,
  env: PaintEnv,
  phase: number,
) {
  if (level <= 0.001) return;
  ctx.save();
  ctx.beginPath();
  ctx.arc(x, y, r, 0, 2 * Math.PI);
  ctx.clip();
  const surface = y + r - 2 * r * Math.min(1, level);
  const amplitude = level >= 0.99 || env.reduced ? 0 : r * 0.07;
  const t = env.now / 650 + phase * 6;
  ctx.beginPath();
  ctx.moveTo(x - r, y + r);
  const steps = 10;
  for (let i = 0; i <= steps; i += 1) {
    const px = x - r + (2 * r * i) / steps;
    ctx.lineTo(px, surface + Math.sin(t + (i / steps) * Math.PI * 2) * amplitude);
  }
  ctx.lineTo(x + r, y + r);
  ctx.closePath();
  ctx.fillStyle = color;
  ctx.fill();
  ctx.restore();
}

export function paintNode(node: GraphNode, ctx: CanvasRenderingContext2D, scale: number, env: PaintEnv) {
  const progress = appearProgress(env, node.id);
  if (progress <= 0) return;
  const x = node.x ?? 0;
  const y = node.y ?? 0;
  const t = clamp01(progress);
  const grow = t >= 1 ? 1 : Math.max(0, easeOutBack(t));
  const base = nodeRadius(node);
  const r = base * grow;
  const { palette } = env;
  const dimmed = env.focusSet !== null && !env.focusSet.has(node.id);
  const fade = (dimmed ? 0.13 : 1) * clamp01(t * 1.8);
  const phase = hashUnit(node.id);
  const hovered = node.id === env.hoverId;

  ctx.save();
  ctx.globalAlpha = fade;

  // 出生时的一圈涟漪；补上缺口的卡片用绿色、圈更大
  const born = env.born.get(node.id);
  if (!env.reduced && born !== undefined && node.kind !== 'ghost') {
    const rt = (env.now - born) / RIPPLE_MS;
    if (rt > 0 && rt < 1) {
      const filled = env.filled.has(node.id);
      ctx.beginPath();
      ctx.arc(x, y, base + 2 + (filled ? 22 : 13) * easeOutCubic(rt), 0, 2 * Math.PI);
      ctx.strokeStyle = filled ? palette.insight : palette.primary;
      ctx.lineWidth = (filled ? 2 : 1.4) / scale;
      ctx.globalAlpha = fade * (1 - rt) * (filled ? 0.75 : 0.45);
      ctx.stroke();
      ctx.globalAlpha = fade;
    }
  }

  switch (node.kind) {
    case 'domain': {
      const breathe = env.reduced ? 0 : Math.sin(env.now / 1100) * 1.6;
      ctx.beginPath();
      ctx.arc(x, y, r + 7 + breathe, 0, 2 * Math.PI);
      ctx.fillStyle = palette.primary;
      ctx.globalAlpha = fade * 0.12;
      ctx.fill();
      ctx.globalAlpha = fade;
      ctx.beginPath();
      ctx.arc(x, y, r, 0, 2 * Math.PI);
      ctx.fillStyle = palette.primary;
      ctx.fill();
      // 外圈：整体掌握度
      const overall = env.level.get(node.id);
      if (typeof overall === 'number') {
        ctx.lineWidth = 2.2 / scale;
        ctx.lineCap = 'round';
        ctx.beginPath();
        ctx.arc(x, y, r + 3.2, 0, 2 * Math.PI);
        ctx.strokeStyle = palette.muted;
        ctx.globalAlpha = fade * 0.22;
        ctx.stroke();
        if (overall > 0.002) {
          ctx.beginPath();
          ctx.arc(x, y, r + 3.2, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * Math.min(1, overall));
          ctx.strokeStyle = masteryColor(palette, overall);
          ctx.globalAlpha = fade;
          ctx.stroke();
        }
      }
      break;
    }
    case 'topic': {
      const level = env.level.get(node.id) ?? 0;
      const count = env.topicCount.get(node.id) ?? 0;
      if (count === 0 && level < 0.01) {
        // 缺口主题：灰色虚线空心球
        ctx.beginPath();
        ctx.arc(x, y, r, 0, 2 * Math.PI);
        ctx.fillStyle = palette.card;
        ctx.fill();
        ctx.fillStyle = palette.muted;
        ctx.globalAlpha = fade * 0.09;
        ctx.fill();
        ctx.globalAlpha = fade * (hovered ? 1 : 0.75);
        ctx.strokeStyle = hovered ? palette.primary : palette.muted;
        ctx.lineWidth = 1.5 / scale;
        dashedCircle(ctx, x, y, r, scale, env, phase);
        if (r * scale > 9) {
          ctx.globalAlpha = fade * 0.6;
          plusMark(ctx, x, y, r * 0.38, scale);
        }
      } else {
        const color = masteryColor(palette, level);
        ctx.beginPath();
        ctx.arc(x, y, r, 0, 2 * Math.PI);
        ctx.fillStyle = palette.card;
        ctx.fill();
        ctx.fillStyle = color;
        ctx.globalAlpha = fade * 0.12;
        ctx.fill();
        ctx.globalAlpha = fade;
        liquidFill(ctx, x, y, r, Math.max(0.12, level), color, env, phase);
        ctx.beginPath();
        ctx.arc(x, y, r, 0, 2 * Math.PI);
        ctx.lineWidth = 1.6 / scale;
        ctx.strokeStyle = color;
        ctx.stroke();
      }
      break;
    }
    case 'ghost': {
      const breathe = env.reduced ? 0.72 : 0.65 + 0.12 * Math.sin(env.now / 1200 + phase * Math.PI * 2);
      const active = hovered || node.id === env.selectedId;
      ctx.beginPath();
      ctx.arc(x, y, r, 0, 2 * Math.PI);
      ctx.fillStyle = active ? palette.primary : palette.muted;
      ctx.globalAlpha = fade * (active ? 0.14 : 0.08);
      ctx.fill();
      ctx.globalAlpha = fade * (active ? 1 : breathe + 0.15);
      ctx.strokeStyle = active ? palette.primary : palette.muted;
      ctx.lineWidth = 1.3 / scale;
      dashedCircle(ctx, x, y, r, scale, env, phase);
      if (r * scale > 4) {
        ctx.globalAlpha = fade * (active ? 0.9 : breathe);
        plusMark(ctx, x, y, r * 0.42, scale);
      }
      break;
    }
    case 'document': {
      const side = r * 1.7;
      ctx.beginPath();
      ctx.roundRect(x - side / 2, y - side / 2, side, side, 2.2);
      if (node.status === 'failed') {
        ctx.fillStyle = palette.danger;
        ctx.fill();
      } else if (node.status && node.status !== 'ready') {
        // 还在处理：空心虚线
        ctx.fillStyle = palette.card;
        ctx.fill();
        ctx.strokeStyle = palette.ai;
        ctx.lineWidth = 1.3 / scale;
        ctx.setLineDash([2 / scale, 1.6 / scale]);
        ctx.stroke();
        ctx.setLineDash([]);
      } else {
        ctx.fillStyle = palette.ai;
        ctx.fill();
        // 一道「书脊」高光，让方块读起来像一份资料
        ctx.fillStyle = palette.card;
        ctx.globalAlpha = fade * 0.35;
        ctx.fillRect(x - side / 2 + side * 0.22, y - side / 2 + 1, side * 0.1, side - 2);
      }
      break;
    }
    case 'insight': {
      ctx.beginPath();
      ctx.moveTo(x, y - r);
      ctx.lineTo(x + r, y);
      ctx.lineTo(x, y + r);
      ctx.lineTo(x - r, y);
      ctx.closePath();
      const color = node.status === 'conflicted' ? palette.danger : palette.insight;
      if (node.status === 'candidate') {
        ctx.fillStyle = palette.card;
        ctx.fill();
        ctx.lineWidth = 1.5 / scale;
        ctx.strokeStyle = color;
        ctx.stroke();
      } else {
        ctx.fillStyle = color;
        ctx.fill();
      }
      break;
    }
    default: {
      if (env.filled.has(node.id) && t < 1) {
        ctx.strokeStyle = palette.muted;
        ctx.lineWidth = 1.3 / scale;
        ctx.globalAlpha = fade * (1 - t);
        dashedCircle(ctx, x, y, 9, scale, env, phase);
      }
      ctx.beginPath();
      ctx.arc(x, y, r, 0, 2 * Math.PI);
      ctx.fillStyle = env.layout === 'type' ? cardKindColor(palette, node.status) : masteryColor(palette, node.mastery);
      ctx.globalAlpha = fade * 0.88;
      ctx.fill();
    }
  }

  if (node.id === env.selectedId) {
    ctx.globalAlpha = 1;
    ctx.beginPath();
    ctx.arc(x, y, base + 3.8, 0, 2 * Math.PI);
    ctx.lineWidth = 1.6 / scale;
    ctx.strokeStyle = palette.fg;
    ctx.stroke();
  } else if (hovered && node.kind !== 'ghost') {
    ctx.globalAlpha = 0.4;
    ctx.beginPath();
    ctx.arc(x, y, base + 3, 0, 2 * Math.PI);
    ctx.lineWidth = 1.2 / scale;
    ctx.strokeStyle = palette.fg;
    ctx.stroke();
  }

  ctx.restore();
}

export function paintLink(link: GraphLink, ctx: CanvasRenderingContext2D, scale: number, env: PaintEnv) {
  const source = link.source as GraphNode;
  const target = link.target as GraphNode;
  if (typeof source !== 'object' || typeof target !== 'object') return;
  if (typeof source.x !== 'number' || typeof target.x !== 'number') return;
  const sourceIn = appearProgress(env, source.id);
  const targetIn = appearProgress(env, target.id);
  if (sourceIn <= 0 || targetIn <= 0) return;

  // 连线从靠中心的一端「长」向新节点
  const born = Math.max(env.born.get(source.id) ?? -Infinity, env.born.get(target.id) ?? -Infinity);
  const grow = env.reduced || !Number.isFinite(born) ? 1 : easeOutCubic(clamp01((env.now - born) / LINK_GROW_MS));
  const sx = source.x;
  const sy = source.y ?? 0;
  const tx = sx + (target.x - sx) * grow;
  const ty = sy + ((target.y ?? 0) - sy) * grow;

  const focused =
    env.focusSet !== null && (source.id === env.focusId || target.id === env.focusId);
  const dimmed = env.focusSet !== null && !focused;
  const crossCluster =
    env.layout !== 'network' && env.cluster.get(source.id) !== env.cluster.get(target.id);
  const { palette } = env;

  ctx.save();
  ctx.beginPath();
  ctx.moveTo(sx, sy);
  ctx.lineTo(tx, ty);
  if (link.kind === 'ghost') {
    ctx.setLineDash([2 / scale, 2.4 / scale]);
    ctx.strokeStyle = focused ? palette.linkStrong : palette.muted;
    ctx.globalAlpha = dimmed ? 0.08 : focused ? 0.9 : 0.32;
  } else {
    if (link.kind === 'insight') ctx.setLineDash([2.5 / scale, 2 / scale]);
    ctx.strokeStyle = focused ? palette.linkStrong : palette.link;
    ctx.globalAlpha = dimmed ? 0.35 : crossCluster && !focused ? 0.45 : 1;
  }
  ctx.lineWidth = (link.kind === 'topic' ? 1.3 : focused ? 1.1 : 0.8) / scale;
  ctx.stroke();
  ctx.setLineDash([]);

  // 生长中的连线头上有一颗亮点：知识正沿着这条线流进来
  if (grow < 1) {
    ctx.beginPath();
    ctx.arc(tx, ty, 1.8, 0, 2 * Math.PI);
    ctx.fillStyle = palette.primary;
    ctx.globalAlpha = 0.85;
    ctx.fill();
  } else if (focused && link.kind !== 'ghost' && !env.reduced) {
    // 聚焦时沿线缓缓流动的小点，看得出方向（中心 → 外侧）
    const phase = (env.now / 1500 + hashUnit(source.id + target.id)) % 1;
    ctx.beginPath();
    ctx.arc(sx + (target.x - sx) * phase, sy + ((target.y ?? 0) - sy) * phase, 1.3, 0, 2 * Math.PI);
    ctx.fillStyle = palette.primary;
    ctx.globalAlpha = 0.7 * Math.sin(phase * Math.PI);
    ctx.fill();
  }
  ctx.restore();
}

/** 分组布局下，每一组画一块淡淡的底，组名写在上方 */
export function paintClusters(ctx: CanvasRenderingContext2D, scale: number, env: PaintEnv) {
  if (env.layout === 'network') {
    env.hulls.clear();
    return;
  }
  const groups = new Map<string, GraphNode[]>();
  for (const node of env.visible) {
    const key = env.cluster.get(node.id);
    if (!key || typeof node.x !== 'number' || appearProgress(env, node.id) <= 0) continue;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(node);
  }
  for (const key of [...env.hulls.keys()]) if (!groups.has(key)) env.hulls.delete(key);

  ctx.save();
  for (const [key, members] of groups) {
    const cx = members.reduce((sum, node) => sum + (node.x ?? 0), 0) / members.length;
    const cy = members.reduce((sum, node) => sum + (node.y ?? 0), 0) / members.length;
    let radius = 0;
    for (const node of members) {
      radius = Math.max(radius, Math.hypot((node.x ?? 0) - cx, (node.y ?? 0) - cy) + nodeRadius(node));
    }
    radius += 12;
    const previous = env.hulls.get(key);
    const hull = previous
      ? {
          x: previous.x + (cx - previous.x) * 0.15,
          y: previous.y + (cy - previous.y) * 0.15,
          r: previous.r + (radius - previous.r) * 0.15,
        }
      : { x: cx, y: cy, r: radius };
    env.hulls.set(key, hull);

    const { color, dashed } = env.clusterColor(key);
    const dimmed = env.focusSet !== null && !members.some((node) => env.focusSet!.has(node.id));
    ctx.globalAlpha = dimmed ? 0.025 : 0.06;
    ctx.beginPath();
    ctx.arc(hull.x, hull.y, hull.r, 0, 2 * Math.PI);
    ctx.fillStyle = color;
    ctx.fill();
    ctx.globalAlpha = dimmed ? 0.08 : 0.3;
    ctx.lineWidth = 1 / scale;
    ctx.strokeStyle = color;
    if (dashed) ctx.setLineDash([4 / scale, 3 / scale]);
    ctx.stroke();
    ctx.setLineDash([]);

    // 按主题分组时，主题自己的名字就是组名，不再重复
    if (key.startsWith('topic:')) continue;
    if (!env.showLabels && !members.some((node) => node.id === env.hoverId)) continue;
    const name = CLUSTER_LABEL[key] ?? key;
    const fontSize = 11.5 / scale;
    ctx.font = `600 ${fontSize}px ${FONT}`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'bottom';
    ctx.globalAlpha = dimmed ? 0.25 : 0.85;
    ctx.fillStyle = color;
    ctx.fillText(`${name} · ${members.length}`, hull.x, hull.y - hull.r - 4 / scale);
  }
  ctx.restore();
}

/**
 * 标签单独一遍画在所有节点之上，按优先级贪心摆放、互相不压：
 * 逐节点画的时候后画的字会压住先画的，主题一圈挤在一起时叠成一团看不清。
 */
export function paintLabels(ctx: CanvasRenderingContext2D, scale: number, env: PaintEnv) {
  const { hoverId, selectedId, palette } = env;
  const placed: Array<[number, number, number, number]> = [];
  const candidates = env.visible
    .filter((node) => {
      if (typeof node.x !== 'number' || typeof node.y !== 'number') return false;
      if (appearProgress(env, node.id) <= 0.3) return false;
      return env.showLabels || node.id === hoverId;
    })
    .sort((a, b) => labelPriority(a, hoverId, selectedId) - labelPriority(b, hoverId, selectedId));

  ctx.save();
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  for (const node of candidates) {
    const strong = node.kind === 'domain' || node.kind === 'topic';
    const fontSize = (node.kind === 'domain' ? 13 : node.kind === 'topic' ? 11 : 10) / scale;
    ctx.font = `${strong ? 600 : 400} ${fontSize}px ${FONT}`;
    const limit = node.kind === 'card' || node.kind === 'ghost' ? 12 : 18;
    const raw = node.kind === 'ghost' ? `待补 · ${node.label}` : node.label;
    const text = raw.length > limit + 4 ? `${raw.slice(0, limit + 3)}…` : raw;
    const width = ctx.measureText(text).width;
    const x = node.x as number;
    const pad = 2 / scale;
    const height = fontSize * 1.25;
    const offset = nodeRadius(node) + 3 / scale;
    const boxAt = (top: number): [number, number, number, number] => [
      x - width / 2 - pad,
      top - pad,
      x + width / 2 + pad,
      top + height + pad,
    ];
    const free = (box: [number, number, number, number]) =>
      !placed.some(([l, t, r, b]) => box[0] < r && box[2] > l && box[1] < b && box[3] > t);
    // 先放节点下方，被占了再试上方
    const below = (node.y as number) + offset;
    const above = (node.y as number) - offset - height;
    const pinned = node.id === hoverId || node.id === selectedId;
    let top = below;
    if (!free(boxAt(below)) && !pinned) {
      if (!free(boxAt(above))) continue;
      top = above;
    }
    placed.push(boxAt(top));
    const fade = clamp01(appearProgress(env, node.id));
    const gap = node.kind === 'ghost' || (node.kind === 'topic' && (env.topicCount.get(node.id) ?? 0) === 0);
    ctx.lineWidth = 3 / scale;
    ctx.lineJoin = 'round';
    ctx.strokeStyle = palette.card;
    ctx.globalAlpha = 0.92 * fade;
    ctx.strokeText(text, x, top);
    ctx.globalAlpha = fade;
    ctx.fillStyle = node.kind === 'card' || gap ? palette.muted : palette.fg;
    ctx.fillText(text, x, top);
  }
  ctx.restore();
}

/** 指针命中区域：比画出来的形状略大一圈，小卡片也好点中 */
export function paintPointerArea(node: GraphNode, color: string, ctx: CanvasRenderingContext2D, env: PaintEnv) {
  if (appearProgress(env, node.id) <= 0.3) return;
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.arc(node.x ?? 0, node.y ?? 0, nodeRadius(node) + 3, 0, 2 * Math.PI);
  ctx.fill();
}
