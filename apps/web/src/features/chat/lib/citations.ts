import type { CitationMarker } from '@/lib/api/types.temp';

/**
 * 一条回答里的引用，按「读者看到的顺序」编号。
 *
 * 后端的标记编号是**检索结果的序号**（c1 = 检索第 1 条），模型只引用其中几条时，
 * 正文里就会出现「1、3」这样跳号的角标——用户会以为漏了第 2 条。这里按引用在正文里
 * 首次出现的位置重新编成 1、2、3…；同一个切片被多个标记指向时共用一个号。
 */
export interface NumberedCitation extends CitationMarker {
  /** 界面上显示的序号，从 1 起连续。 */
  n: number;
}

export interface CitationNumbering {
  /** 去重后的引用，按显示序号排列。 */
  items: NumberedCitation[];
  /** 原始标记（c3）→ 显示序号。 */
  byMarker: Map<string, number>;
  /** 切片 id → 显示序号；证据栏用它和回答里的角标对齐。 */
  byChunk: Map<string, number>;
}

function normalizeMarker(marker: string): string {
  const digits = marker.replace(/^\^?c?/i, '');
  return digits ? `c${digits}` : marker;
}

export function numberCitations(
  citations?: readonly CitationMarker[] | null,
): CitationNumbering {
  const byMarker = new Map<string, number>();
  const byChunk = new Map<string, number>();
  const items: NumberedCitation[] = [];
  if (!citations || citations.length === 0) return { items, byMarker, byChunk };

  // 稳定排序：有位置的按位置，没位置的（老数据、插不回正文的）保持原顺序排在后面
  const ordered = citations
    .map((citation, index) => ({ citation, index }))
    .sort((left, right) => {
      const a = left.citation.char_offset;
      const b = right.citation.char_offset;
      const hasA = typeof a === 'number';
      const hasB = typeof b === 'number';
      if (hasA && hasB && a !== b) return (a as number) - (b as number);
      if (hasA !== hasB) return hasA ? -1 : 1;
      return left.index - right.index;
    });

  for (const { citation } of ordered) {
    const key = citation.chunk_id || citation.marker;
    let n = byChunk.get(key);
    if (n === undefined) {
      n = items.length + 1;
      byChunk.set(key, n);
      items.push({ ...citation, n });
    }
    byMarker.set(normalizeMarker(citation.marker), n);
  }
  return { items, byMarker, byChunk };
}

/** 按原始标记查显示序号；查不到（模型编出来的号）返回 null。 */
export function displayNumberOf(
  numbering: CitationNumbering,
  marker: string,
): number | null {
  return numbering.byMarker.get(normalizeMarker(marker)) ?? null;
}

function bigrams(value: string): Set<string> {
  const text = value.replace(/[^\p{L}\p{N}]/gu, '').toLowerCase();
  const grams = new Set<string>();
  for (let index = 0; index < text.length - 1; index += 1) grams.add(text.slice(index, index + 2));
  return grams;
}

/**
 * 一级标题是不是「文档名」：用户起的标题常是正文 H1 的简写
 * （「星澜 X3 技术手册」vs「星澜 X3 储能系统技术手册」），逐字相等判断不出来。
 */
function looksLikeTitle(heading: string, title: string): boolean {
  if (heading === title) return true;
  const a = bigrams(heading);
  const b = bigrams(title);
  if (a.size === 0 || b.size === 0) return false;
  let shared = 0;
  for (const gram of a) if (b.has(gram)) shared += 1;
  return shared / Math.min(a.size, b.size) >= 0.6;
}

/**
 * 章节路径去掉与文档标题相同的第一级：Markdown 的一级标题通常就是文档名，
 * 「长江大学建立时间 › 长江大学建立时间 › 历史沿革」这种重复只会挤掉真正有用的那一级。
 */
export function sectionOf(
  headingPath?: string | null,
  documentTitle?: string | null,
): string | null {
  if (!headingPath) return null;
  const parts = headingPath
    .split('>')
    .map((part) => part.trim())
    .filter(Boolean);
  const title = (documentTitle || '').replace(/\.(md|markdown|txt|pdf|docx?|html?)$/i, '').trim();
  while (parts.length > 1 && title && looksLikeTitle(parts[0].replace(/^#+\s*/, ''), title)) {
    parts.shift();
  }
  if (parts.length === 1 && title && parts[0] === title) return null;
  return parts.join(' › ') || null;
}

interface LocatableCitation {
  document_title?: string | null;
  heading_path?: string | null;
  page?: number | null;
  ordinal?: number | null;
  kind?: string | null;
}

/** 分页文档才有「第几页」的意义；Markdown / 网页的 page 一律是 1，显示出来只是噪音。 */
function isPaged(citation: LocatableCitation): boolean {
  if (typeof citation.page !== 'number') return false;
  return citation.page > 1 || /\.(pdf|docx?|pptx?)$/i.test(citation.document_title || '');
}

/**
 * 引用的原文位置，拆成几段短标签：「第 3 页」「1. 化合物库清单」「第 4 段」。
 * 概要切片没有原文区间，只说明它是概要。
 */
export function locationParts(citation: LocatableCitation): string[] {
  if (citation.kind === 'summary') return ['文档概要'];
  const parts: string[] = [];
  if (isPaged(citation)) parts.push(`第 ${citation.page} 页`);
  const section = sectionOf(citation.heading_path, citation.document_title);
  if (section) parts.push(section);
  if (typeof citation.ordinal === 'number') parts.push(`第 ${citation.ordinal + 1} 段`);
  return parts;
}

export function describeLocation(citation: LocatableCitation): string {
  return locationParts(citation).join(' · ');
}

/** 引用 / 命中里「依据原句」的全文区间；没有收窄过的老数据返回 null。 */
export function quoteRangeOf(target: {
  quote_start?: number | null;
  quote_end?: number | null;
}): { start: number; end: number } | null {
  const { quote_start: start, quote_end: end } = target;
  return typeof start === 'number' && typeof end === 'number' && end > start
    ? { start, end }
    : null;
}
