/**
 * 切片高亮：在渲染好的 Markdown 里把某个切片覆盖的块标上底色。
 *
 * 从阅读器组件里抽出来单独成文：这几个函数只跟 DOM 打交道、没有 React 依赖，
 * 抽出来才测得了——它们正是「引用点进来定位到哪一段」的判据。
 */

/** 高亮底色。带 `!` 是必须的：表格行自带 `hover:bg-muted/40`，鼠标一指上去就会把
 *  蓝色盖成灰色——看起来就像高亮消失了。 */
export const HL_CLASS = '!bg-primary/25 dark:!bg-primary/30';

/** 高亮区间的首尾与左侧竖条，用来把范围框出来。 */
export const HL_FIRST_CLASS = 'border-t-2 border-primary/70';
export const HL_LAST_CLASS = 'border-b-2 border-primary/70';
export const HL_SIDE_CLASS = 'border-l-2 border-primary/70';

/** 标记属性：每次重新高亮前按它清掉上一轮，保证幂等。 */
export const HL_ATTR = 'data-chunk-highlight';

/** 参与高亮的最小单元：表格按行、列表按项，其余按块。单元格单独拿出来会让探测片段
 *  跨列时匹配不上，所以表格以 tr 为单位。 */
export const HL_BLOCKS = 'p, li, h1, h2, h3, h4, h5, h6, pre, blockquote, tr';

/** 只保留文字与数字：Markdown 的记号（# | * > 等）在渲染后就不存在了。 */
export function plainText(value: string): string {
  return value.replace(/[^\p{L}\p{N}]/gu, '');
}

/**
 * 把选中切片覆盖的块级元素标上底色。
 *
 * 整篇 Markdown 只渲染一次（结构因此完整），高亮在渲染后按**块**处理：用切片首尾
 * 的文字片段在渲染结果里定位起止块，把它们（表格按行、列表按项）标上底色。
 * 不往源码里插标记——那会破坏 `#`、`|`、`-` 这类行首记号，把标题变成段落、把表格
 * 变成一串竖线。
 */
export function highlightBlocks(
  root: HTMLElement,
  headProbe: string,
  tailProbe: string,
): HTMLElement | null {
  const blocks = Array.from(root.querySelectorAll<HTMLElement>(HL_BLOCKS)).filter(
    (element) => !element.closest('pre') || element.tagName === 'PRE',
  );
  const headText = plainText(headProbe);
  const tailText = plainText(tailProbe);
  if (!headText) return null;

  // 逐个试更短的片段：切片常从表格中间开始，一次取 16 个字会跨过整行，反而匹配不上
  const candidates = (text: string, fromEnd: boolean): string[] => {
    const lengths = [16, 12, 9, 6, 4];
    return lengths
      .map((length) => (fromEnd ? text.slice(-length) : text.slice(0, length)))
      .filter((value) => value.length >= 4);
  };
  const findBlock = (probes: string[]): number => {
    for (const probe of probes) {
      const index = blocks.findIndex((element) =>
        plainText(element.textContent ?? '').includes(probe),
      );
      if (index >= 0) return index;
    }
    return -1;
  };

  const unit = (element: HTMLElement): HTMLElement => element.closest('tr') ?? element;

  const first = findBlock(candidates(headText, false));
  if (first < 0) return null;
  const tailIndex = findBlock(candidates(tailText, true));
  let last = tailIndex >= first ? tailIndex : first;
  if (last < first) last = first;

  const touched = new Set<HTMLElement>();
  const units: HTMLElement[] = [];
  for (let index = first; index <= last; index += 1) {
    const target = unit(blocks[index]);
    if (touched.has(target)) continue;
    touched.add(target);
    units.push(target);
  }
  if (units.length === 0) return null;

  units.forEach((target, index) => {
    target.setAttribute(HL_ATTR, '1');
    target.classList.add(...HL_CLASS.split(' '));
    if (!target.tagName.startsWith('T')) {
      // 段落、列表项、代码块加左侧竖条；表格行加竖条会破坏表格边框
      target.classList.add(...HL_SIDE_CLASS.split(' '));
    }
    if (index === 0) target.classList.add(...HL_FIRST_CLASS.split(' '));
    if (index === units.length - 1) target.classList.add(...HL_LAST_CLASS.split(' '));
  });
  return units[0];
}

/** 清掉上一轮的高亮标记，重复执行不会越描越深。 */
export function clearHighlights(root: HTMLElement): void {
  root.querySelectorAll<HTMLElement>(`[${HL_ATTR}]`).forEach((element) => {
    element.removeAttribute(HL_ATTR);
    element.classList.remove(
      ...HL_CLASS.split(' '),
      ...HL_SIDE_CLASS.split(' '),
      ...HL_FIRST_CLASS.split(' '),
      ...HL_LAST_CLASS.split(' '),
    );
  });
}

/** CSS Custom Highlight 的名字，样式见 index.css 的 `::highlight(agentmem-quote)`。 */
export const QUOTE_HIGHLIGHT = 'agentmem-quote';

type HighlightRegistry = Map<string, unknown>;
type HighlightCtor = new (...ranges: Range[]) => unknown;

function highlightApi(): { registry: HighlightRegistry; Highlight: HighlightCtor } | null {
  const registry = (globalThis.CSS as unknown as { highlights?: HighlightRegistry } | undefined)
    ?.highlights;
  const Highlight = (globalThis as unknown as { Highlight?: HighlightCtor }).Highlight;
  return registry && Highlight ? { registry, Highlight } : null;
}

/**
 * 在渲染好的正文里找到引用依据的那一句，返回它的 DOM Range。
 *
 * 引用原句取自 Markdown 源码（可能带 `**`、`|` 之类记号），渲染后的文字里没有这些，
 * 所以两边都只比文字与数字，再把命中的位置映射回具体的文本节点。
 */
export function findQuoteRange(root: HTMLElement, quote: string): Range | null {
  const target = plainText(quote);
  if (target.length < 4) return null;
  const positions: Array<[Text, number]> = [];
  let flat = '';
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const text = node as Text;
    const value = text.data;
    for (let index = 0; index < value.length; index += 1) {
      const char = value[index];
      if (/[\p{L}\p{N}]/u.test(char)) {
        flat += char;
        positions.push([text, index]);
      }
    }
  }
  const at = flat.indexOf(target);
  if (at < 0) return null;
  const [startNode, startOffset] = positions[at];
  const [endNode, endOffset] = positions[at + target.length - 1];
  const range = document.createRange();
  range.setStart(startNode, startOffset);
  range.setEnd(endNode, endOffset + 1);
  return range;
}

/** 给原句画上精确到字的高亮；浏览器不支持 Highlight API 时静默跳过（块级高亮仍在）。 */
export function highlightQuote(root: HTMLElement, quote: string): Range | null {
  clearQuoteHighlight();
  const range = findQuoteRange(root, quote);
  const api = highlightApi();
  if (range && api) api.registry.set(QUOTE_HIGHLIGHT, new api.Highlight(range));
  return range;
}

export function clearQuoteHighlight(): void {
  highlightApi()?.registry.delete(QUOTE_HIGHLIGHT);
}
