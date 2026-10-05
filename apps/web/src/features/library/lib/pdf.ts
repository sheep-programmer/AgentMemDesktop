/**
 * 原版 PDF 视图的纯逻辑：判断来源、切片 → 页码、文字层匹配、按需渲染的页窗口。
 *
 * 和 pdf.js 本身无关的部分都放在这里，便于单测；组件只负责把结果画到页面上。
 */
import { markdownToPlainText } from '@/lib/markdown';

/** 判断 PDF 需要的最少字段：列表项、详情、只带 id 的占位对象都能传进来。 */
export interface PdfSourceLike {
  mime?: string | null;
  title?: string | null;
  source_uri?: string | null;
  meta?: { raw_path?: string | null } | null;
}

const PDF_EXT = /\.pdf$/i;

/**
 * 这篇文档的原始文件是不是 PDF。
 *
 * 优先看 mime；上传时浏览器没给类型（或给了 octet-stream）的老数据退回看扩展名。
 * 网页抓取、粘贴文本即使标题以 .pdf 结尾也不算——它们没有可渲染的原始 PDF。
 */
export function isPdfDocument(doc: PdfSourceLike | null | undefined): boolean {
  if (!doc) return false;
  const mime = (doc.mime ?? '').toLowerCase().split(';')[0].trim();
  if (mime === 'application/pdf' || mime === 'application/x-pdf') return true;
  if (mime && mime !== 'application/octet-stream') return false;
  const candidates = [doc.meta?.raw_path, doc.source_uri, doc.title];
  return candidates.some((value) => typeof value === 'string' && PDF_EXT.test(value.trim()));
}

/** 切片定位需要的最少字段。 */
export interface PagedChunkLike {
  id: string;
  ordinal?: number;
  page?: number | null;
  kind?: string;
}

export interface ChunkPage {
  page: number;
  /** false：切片自己没有页码，借用了前面最近一个有页码的切片（只能说「大约在这页」） */
  exact: boolean;
}

const validPage = (page: number | null | undefined): page is number =>
  typeof page === 'number' && Number.isFinite(page) && page >= 1;

/**
 * 切片所在的页码（从 1 开始，与 pdf.js 一致）。
 *
 * 自己有页码就用自己的；没有时往前找最近的正文切片借一个页码，并标成「不精确」——
 * 切片是按顺序切出来的，前一片所在页是它起点的下界，比随手估一个比例靠谱。
 * 概要切片不对应原文位置，一律返回 null。
 */
export function resolveChunkPage(
  chunks: readonly PagedChunkLike[],
  chunkId: string | null | undefined,
): ChunkPage | null {
  if (!chunkId) return null;
  const index = chunks.findIndex((chunk) => chunk.id === chunkId);
  if (index < 0) return null;
  const target = chunks[index];
  if (target.kind === 'summary') return null;
  if (validPage(target.page)) return { page: Math.floor(target.page), exact: true };
  for (let cursor = index - 1; cursor >= 0; cursor -= 1) {
    const prev = chunks[cursor];
    if (prev.kind === 'summary') continue;
    if (validPage(prev.page)) return { page: Math.floor(prev.page), exact: false };
  }
  return null;
}

export function clampPage(page: number, total: number): number {
  if (total <= 0) return 1;
  return Math.min(Math.max(1, Math.round(page)), total);
}

/**
 * 需要保持渲染的页：可见页各自向前后扩 `buffer` 页，再加上当前目标页。
 *
 * 其余页只留一个等高的占位框，几百页的文档也只有十来张画布在内存里。
 */
export function computeRenderWindow(
  visiblePages: Iterable<number>,
  total: number,
  buffer = 1,
  anchorPage?: number | null,
): Set<number> {
  const result = new Set<number>();
  if (total <= 0) return result;
  const add = (center: number) => {
    for (let page = center - buffer; page <= center + buffer; page += 1) {
      if (page >= 1 && page <= total) result.add(page);
    }
  };
  for (const page of visiblePages) add(page);
  if (anchorPage != null) add(clampPage(anchorPage, total));
  return result;
}

/**
 * 匹配用的归一化：只留字母与数字，统一大小写与兼容字形。
 *
 * PDF 文字层的空格、换行连字符、连字（ﬁ）都和解析出的 Markdown 对不上，
 * 标点与空白一律丢掉之后两边才可比。
 */
export function normalizeForMatch(text: string): string {
  return text
    .normalize('NFKC')
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, '');
}

export interface TextLayerMatch {
  /** 命中的文字层条目下标（与 TextLayer.textDivs 一一对应），升序 */
  indices: number[];
  /** 切片正文里有多少比例在这一页上找到了（0–1） */
  coverage: number;
}

/** 探测窗口的长度：太短会在公式、表格数字里误撞，太长又容不下两边的细小差异。 */
const PROBE = 16;
/** 一页上至少要对上这么多比例的切片正文才算命中，低于它宁可不标。 */
const MIN_COVERAGE = 0.25;
/** 相邻两个命中之间，页面文字最多可以比切片多出这么多字（页眉、图注、脚注插在中间）。 */
const MAX_INSERT = 400;
/** 往回最多连几个窗口：中间几个窗口没对上（公式被改写）不至于断链。 */
const MAX_SKIP = 8;
/** 一个窗口在页面上最多考虑这么多处出现：套话再多也不至于拖慢。 */
const MAX_OCCURRENCES = 64;
/** 续页模式下，切片后半截必须从页面文字的前这么多字里开始（留出页眉的位置）。 */
const CONTINUATION_HEAD = 600;

export interface MatchOptions {
  /**
   * 续页：切片从上一页延续过来，这一页上只可能有它的**后半截**，而且在页面文字的开头。
   * 不加这层约束的话，套话式的重复文字会让下一页随便哪一段都「对上」。
   */
  continuation?: boolean;
}

interface Hit {
  /** 窗口在切片里的起点 */
  start: number;
  length: number;
  /** 在页面文字里的位置 */
  at: number;
}

/**
 * 在一页的文字层条目里找切片正文，返回要高亮的条目。
 *
 * 做法是把切片（去掉 Markdown 记号后）切成定长小窗，找出每个窗口在页面文字里的所有
 * 出现处，再挑一条「对齐链」：链上相邻两个命中在页面里的间距，要和它们在切片里的间距
 * 基本一致（允许页面多出页眉、图注这类插入）。命中最多、间距最贴合的那条链就是切片
 * 真正所在的位置，取它的首尾，中间的条目整段标上。
 *
 * 不能按顺序贪心地找第一处：论文里「本研究表明……」这类句式反复出现，贪心会从前面
 * 某一段的同样开头起标，把好几段都染上（实测双栏样例里就是这样）。
 *
 * 切片跨页时，它在这一页上只有一部分——只要那部分够长，照样能标出来。
 */
export function matchChunkInTextItems(
  items: readonly string[],
  chunkContent: string,
  options: MatchOptions = {},
): TextLayerMatch | null {
  const needle = normalizeForMatch(markdownToPlainText(chunkContent));
  if (!needle) return null;

  // 页面文字拼成一条，并记住每个字符来自哪个条目
  let haystack = '';
  const owner: number[] = [];
  items.forEach((raw, index) => {
    const normalized = normalizeForMatch(raw ?? '');
    haystack += normalized;
    for (let i = 0; i < normalized.length; i += 1) owner.push(index);
  });
  if (!haystack) return null;

  // 定长切窗；切剩的零头不单独成窗（太短容易撞上无关文字），改用末尾一个整窗兜住，
  // 否则切片最后几个字永远标不上
  const probe = Math.min(PROBE, needle.length);
  const windows: Array<{ start: number; text: string }> = [];
  for (let start = 0; start + probe <= needle.length; start += probe) {
    windows.push({ start, text: needle.slice(start, start + probe) });
  }
  if (needle.length % probe !== 0) {
    windows.push({ start: needle.length - probe, text: needle.slice(-probe) });
  }

  // 每个窗口的所有出现处
  const groups: Hit[][] = windows.map(({ start, text }) => {
    const hits: Hit[] = [];
    let from = 0;
    while (hits.length < MAX_OCCURRENCES) {
      const at = haystack.indexOf(text, from);
      if (at < 0) break;
      hits.push({ start, length: text.length, at });
      from = at + 1;
    }
    return hits;
  });

  // 动态规划找对齐链：score 先比命中字数，再比间距偏差（越小越贴合）
  interface Node {
    hit: Hit;
    matched: number;
    drift: number;
    head: Hit;
  }
  const nodes: Node[][] = groups.map(() => []);
  let best: Node | null = null;
  const better = (a: Node, b: Node | null) =>
    !b || a.matched > b.matched || (a.matched === b.matched && a.drift < b.drift);

  for (let w = 0; w < groups.length; w += 1) {
    for (const hit of groups[w]) {
      let node: Node = { hit, matched: hit.length, drift: 0, head: hit };
      for (let prev = w - 1; prev >= 0 && prev >= w - MAX_SKIP; prev -= 1) {
        for (const before of nodes[prev]) {
          const expected = hit.start - before.hit.start;
          const gap = hit.at - before.hit.at;
          const delta = gap - expected;
          // 页面可以多出插入的文字，但不能倒着走，也不能相距太远
          if (gap <= 0 || delta < -probe || delta > MAX_INSERT) continue;
          // 末尾兜底窗口与前一个窗口有重叠，只算新增的那部分
          const gained = Math.min(hit.length, hit.start + hit.length - (before.hit.start + before.hit.length));
          const candidate: Node = {
            hit,
            matched: before.matched + Math.max(0, gained),
            drift: before.drift + Math.abs(delta),
            head: before.head,
          };
          if (better(candidate, node)) node = candidate;
        }
      }
      nodes[w].push(node);
      if (better(node, best)) best = node;
    }
  }

  const chain = best as Node | null;
  if (!chain) return null;
  const first = chain.head.at;
  const last = chain.hit.at + chain.hit.length;
  const coverage = Math.min(1, chain.matched / needle.length);

  if (options.continuation) {
    // 续页上的那一截必须从页首附近开始，并且一直延续到切片末尾
    const reachesEnd = chain.hit.start + chain.hit.length >= needle.length - probe;
    if (first > CONTINUATION_HEAD || !reachesEnd) return null;
  }

  // 跨页切片在这一页上可能只占一小截：比例不够时，连续对上几个窗口也算数
  if (coverage < MIN_COVERAGE && chain.matched < PROBE * 3) return null;

  const indices: number[] = [];
  for (let index = owner[first]; index <= owner[last - 1]; index += 1) {
    if (normalizeForMatch(items[index] ?? '')) indices.push(index);
  }
  return indices.length > 0 ? { indices, coverage } : null;
}

/**
 * 从 pdf.js 的加载错误里认出「原始文件不存在」（后端 404）。
 *
 * v6 统一抛 ResponseException（带 status 与 missing）；老版本的 MissingPDFException
 * 也认，免得升级 pdf.js 时这条提示悄悄退化成「加载失败」。
 */
export function isMissingFileError(error: unknown): boolean {
  if (!error || typeof error !== 'object') return false;
  const { status, missing, name } = error as { status?: unknown; missing?: unknown; name?: unknown };
  return status === 404 || missing === true || name === 'MissingPDFException';
}
