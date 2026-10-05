import type { CitationMarker, Message } from '@/lib/api/types.temp';
import { markdownToPlainText, withCitationMarkers } from '@/lib/markdown';
import { DEFAULT_CONVERSATION_TITLE } from './conversationTitle';

/** 引用摘录在文末最多保留的字数：整段切片贴进来，来源列表会比正文还长。 */
const SNIPPET_MAX_LENGTH = 200;

export interface ConversationExportInput {
  title?: string | null;
  messages: Message[];
  spaceName?: string | null;
  /** 导出时间，默认当前时间；测试里传固定值。 */
  exportedAt?: Date;
}

function pad(n: number): string {
  return String(n).padStart(2, '0');
}

function formatDateTime(date: Date): string {
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}`
  );
}

/** 同一个切片在多轮回答里被引用，文末只列一次、用同一个编号。 */
function sourceKey(citation: CitationMarker): string {
  return citation.chunk_id || `${citation.document_id}#${citation.marker}`;
}

function snippetLine(snippet: string | null | undefined): string {
  if (!snippet) return '';
  // 切片是原文 Markdown（表格、列表、代码块）；脚注只能放一行，压成纯文本再截断
  const plain = markdownToPlainText(snippet) || snippet.replace(/\s+/g, ' ').trim();
  return plain.length > SNIPPET_MAX_LENGTH ? `${plain.slice(0, SNIPPET_MAX_LENGTH)}…` : plain;
}

function describeSource(citation: CitationMarker): string {
  const parts = [`《${citation.document_title || '未命名文献'}》`];
  if (typeof citation.page === 'number') parts.push(`第 ${citation.page} 页`);
  if (citation.kind === 'summary') parts.push('文档概要');
  const head = parts.join(' · ');
  const snippet = snippetLine(citation.snippet);
  return snippet ? `${head}：「${snippet}」` : head;
}

/**
 * 把一个会话导出成 Markdown：每轮的提问与回答，回答里保留引用角标，文末列出引用来源。
 *
 * 两个坑：
 * 1. 后端存的正文已经把 `[^c1]` 剥掉了（只留 `char_offset`），直接导出 `content`
 *    会丢掉「哪句话有出处」。这里和对话页一样按位置把标记插回去。
 * 2. 每条回答的标记都从 `c1` 编起。原样写成脚注，第二轮的 `[^c1]` 会和第一轮撞名，
 *    渲染器只认一个定义，另一轮的出处就指错了。所以整篇按出现顺序重新编号，
 *    同一个切片不论被哪一轮引用都用同一个号。
 *
 * 用 GFM 脚注（`[^1]` / `[^1]: …`）：Typora、Obsidian、GitHub 都能渲染成可点击的引用，
 * 纯文本里也看得懂。
 */
export function buildConversationMarkdown({
  title,
  messages,
  spaceName,
  exportedAt = new Date(),
}: ConversationExportInput): string {
  const numbers = new Map<string, number>();
  const sources: CitationMarker[] = [];
  const numberOf = (citation: CitationMarker): number => {
    const key = sourceKey(citation);
    let n = numbers.get(key);
    if (n === undefined) {
      n = sources.length + 1;
      numbers.set(key, n);
      sources.push(citation);
    }
    return n;
  };

  const renderAnswer = (message: Message): string => {
    const citations = message.citations || [];
    const renumbered = citations.map((c) => ({
      marker: String(numberOf(c)),
      char_offset: c.char_offset,
    }));
    let body = withCitationMarkers(message.content, renumbered).trim();
    if (!body) body = '_（本次生成未返回内容）_';
    // 没有位置信息（或位置越界）的引用插不回正文：挂在回答末尾，
    // 否则文末的脚注定义没人引用，多数渲染器会直接把它丢掉
    const missing = [...new Set(renumbered.map((c) => c.marker))].filter(
      (n) => !body.includes(`[^${n}]`),
    );
    if (missing.length > 0) body += ` ${missing.map((n) => `[^${n}]`).join('')}`;
    return body;
  };

  const turns: string[] = [];
  let current: string[] | null = null;
  const flush = () => {
    if (current) turns.push(current.join('\n\n'));
    current = null;
  };
  for (const message of messages) {
    if (message.role === 'user') {
      flush();
      current = [`## 第 ${turns.length + 1} 轮`, '**提问**', message.content.trim() || '_（空）_'];
    } else if (message.role === 'assistant') {
      // 会话开头就是回答（极少见，比如提问写库失败）也照样导出，不吞内容
      if (!current) current = [`## 第 ${turns.length + 1} 轮`];
      current.push('**回答**', renderAnswer(message));
    }
    // system / tool 消息是内部过程，不进导出
  }
  flush();

  const heading = (title || '').trim() || DEFAULT_CONVERSATION_TITLE;
  const meta = [
    '导出自 AgentMem',
    spaceName ? `知识空间「${spaceName}」` : null,
    formatDateTime(exportedAt),
    `共 ${turns.length} 轮问答`,
  ]
    .filter(Boolean)
    .join(' · ');

  const parts = [`# ${heading}`, `> ${meta}`];
  if (turns.length === 0) {
    parts.push('_这个对话还没有内容。_');
  } else {
    parts.push(turns.join('\n\n---\n\n'));
  }
  if (sources.length > 0) {
    parts.push(
      '## 引用来源',
      sources.map((c, index) => `[^${index + 1}]: ${describeSource(c)}`).join('\n'),
    );
  }
  return `${parts.join('\n\n')}\n`;
}

/** 下载用的文件名：会话标题去掉文件系统不认的字符，附上日期。 */
export function conversationExportFileName(title: string | null | undefined, date = new Date()): string {
  const safe = (title || '')
    .replace(/[\\/:*?"<>|]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 60);
  const day = `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}`;
  return `${safe || DEFAULT_CONVERSATION_TITLE}-${day}.md`;
}
