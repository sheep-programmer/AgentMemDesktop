/**
 * 把 Markdown 压成一行纯文本，供列表里的摘要使用。
 *
 * 卡片正文与经验文本都是 Markdown（模型产出或人工录入），列表里直接显示会露出
 * `##`、`**`、表格竖线这些记号；列表只是索引，完整排版留给详情里的渲染视图。
 */
export function markdownToPlainText(source: string): string {
  if (!source) return '';
  return source
    .replace(/```[\s\S]*?```/g, ' ')          // 代码块整段丢掉
    .replace(/`([^`]*)`/g, '$1')              // 行内代码留内容
    .replace(/!\[[^\]]*\]\([^)]*\)/g, ' ')    // 图片
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')  // 链接留文字
    .replace(/^\s{0,3}#{1,6}\s*/gm, '')       // 标题记号
    .replace(/^\s{0,3}>\s?/gm, '')            // 引用记号
    .replace(/^\s{0,3}[-*+]\s+/gm, '')        // 无序列表记号
    .replace(/^\s{0,3}\d+\.\s+/gm, '')        // 有序列表记号
    .replace(/^\s*\|.*\|\s*$/gm, ' ')         // 表格行（含分隔行）
    .replace(/^\s*[-*_]{3,}\s*$/gm, ' ')      // 分隔线
    .replace(/\*\*([^*]*)\*\*/g, '$1')        // 加粗
    .replace(/(^|[^*])\*([^*]+)\*/g, '$1$2')  // 斜体
    .replace(/~~([^~]*)~~/g, '$1')            // 删除线
    .replace(/\[\^?[a-zA-Z0-9_-]+\]/g, '')    // 引用标记
    .replace(/\s+/g, ' ')
    .trim();
}

/**
 * 引用角标的显示文案。
 *
 * 正文里的标记是 `[^c1]` 这种脚注写法，直接摆到界面上没人看得懂；这里只取序号，
 * 显示成「1」并配上「引用」的说明。
 */
export function citationLabel(marker: string): string {
  const digits = marker.replace(/^\^?c?/i, '');
  return digits || marker;
}

/**
 * 把引用芯片按位置插回正文。
 *
 * 后端在流式解析时把 `[^c1]` 从正文里剥离了（只保留位置，便于按句子统计依归度），
 * 所以正文本身没有标记——不插回去的话，用户只看得到底部一排引用，正文里
 * 哪句话有出处完全看不出来。
 */
export function withCitationMarkers(
  content: string,
  citations?: Array<{ marker: string; char_offset?: number | null }>,
): string {
  if (!citations || citations.length === 0 || !content) return content;

  const points = citations
    .filter(
      (item) =>
        typeof item.char_offset === 'number' &&
        item.char_offset >= 0 &&
        item.char_offset <= content.length,
    )
    .sort((left, right) => (left.char_offset as number) - (right.char_offset as number));
  if (points.length === 0) return content;

  let out = '';
  let cursor = 0;
  let placed = new Set<string>();
  for (const item of points) {
    const at = item.char_offset as number;
    if (at > cursor) {
      out += content.slice(cursor, at);
      cursor = at;
      placed = new Set<string>();
    }
    // 同一处挂了两个指向同一来源的标记（重新编号后合并的切片）只出一枚角标
    if (placed.has(item.marker)) continue;
    placed.add(item.marker);
    out += `[^${item.marker}]`;
  }
  return out + content.slice(cursor);
}
