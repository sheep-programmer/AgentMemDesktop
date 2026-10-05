import { describe, expect, it } from 'vitest';
import type { CitationMarker, Message } from '@/lib/api/types.temp';
import { buildConversationMarkdown, conversationExportFileName } from './exportMarkdown';

const AT = new Date(2026, 8, 28, 14, 3);

let seq = 0;
function msg(role: Message['role'], content: string, citations?: CitationMarker[]): Message {
  seq += 1;
  return { id: `m${seq}`, conversation_id: 'conv', role, content, citations, created_at: seq };
}

function cite(marker: string, chunk: string, extra: Partial<CitationMarker> = {}): CitationMarker {
  return {
    marker,
    chunk_id: chunk,
    document_id: `doc-${chunk}`,
    document_title: `文献-${chunk}`,
    ...extra,
  };
}

describe('buildConversationMarkdown', () => {
  it('按轮次导出提问与回答，标题与元信息在最前', () => {
    const md = buildConversationMarkdown({
      title: 'hERG 阈值',
      spaceName: '新药研发',
      exportedAt: AT,
      messages: [msg('user', '阈值是多少？'), msg('assistant', '一般取 10 μM。')],
    });
    expect(md.startsWith('# hERG 阈值\n\n> 导出自 AgentMem · 知识空间「新药研发」 · 2026-09-28 14:03 · 共 1 轮问答')).toBe(true);
    expect(md).toContain('## 第 1 轮\n\n**提问**\n\n阈值是多少？\n\n**回答**\n\n一般取 10 μM。');
    expect(md).not.toContain('## 引用来源');
  });

  it('按 char_offset 把引用标记插回正文，文末列出标题、页码与摘录', () => {
    const md = buildConversationMarkdown({
      title: 't',
      exportedAt: AT,
      messages: [
        msg('user', '问'),
        msg('assistant', '第一句。第二句。', [
          cite('c1', 'a', { char_offset: 4, page: 3, snippet: '原文 **摘录**\n第二行' }),
          cite('c2', 'b', { char_offset: 8 }),
        ]),
      ],
    });
    expect(md).toContain('第一句。[^1]第二句。[^2]');
    expect(md).toContain('## 引用来源\n\n[^1]: 《文献-a》 · 第 3 页：「原文 摘录 第二行」\n[^2]: 《文献-b》');
  });

  it('多轮回答各自从 c1 编号：整篇重新编号，同一切片共用一个号', () => {
    const md = buildConversationMarkdown({
      title: 't',
      exportedAt: AT,
      messages: [
        msg('user', '一'),
        msg('assistant', '甲乙', [cite('c1', 'a', { char_offset: 1 }), cite('c2', 'b', { char_offset: 2 })]),
        msg('user', '二'),
        msg('assistant', '丙丁', [cite('c1', 'c', { char_offset: 1 }), cite('c2', 'a', { char_offset: 2 })]),
      ],
    });
    expect(md).toContain('甲[^1]乙[^2]');
    expect(md).toContain('丙[^3]丁[^1]');
    expect(md.match(/^\[\^\d+\]: /gm)).toHaveLength(3);
    expect(md).toContain('## 第 2 轮');
    expect(md).toContain('\n\n---\n\n## 第 2 轮');
  });

  it('没有位置的引用挂在回答末尾，保证脚注有人引用', () => {
    const md = buildConversationMarkdown({
      title: 't',
      exportedAt: AT,
      messages: [
        msg('user', '问'),
        msg('assistant', '答案', [cite('c1', 'a'), cite('c2', 'b', { char_offset: 99 })]),
      ],
    });
    expect(md).toContain('答案 [^1][^2]');
  });

  it('摘录过长时截断', () => {
    const md = buildConversationMarkdown({
      title: 't',
      exportedAt: AT,
      messages: [msg('user', '问'), msg('assistant', '答', [cite('c1', 'a', { snippet: '长'.repeat(500) })])],
    });
    expect(md).toContain(`「${'长'.repeat(200)}…」`);
  });

  it('空回答、系统消息与空会话都有交代', () => {
    const md = buildConversationMarkdown({
      title: '',
      exportedAt: AT,
      messages: [msg('system', '内部提示'), msg('user', '问'), msg('assistant', '')],
    });
    expect(md.startsWith('# 新对话')).toBe(true);
    expect(md).not.toContain('内部提示');
    expect(md).toContain('_（本次生成未返回内容）_');

    const empty = buildConversationMarkdown({ title: 'x', exportedAt: AT, messages: [] });
    expect(empty).toContain('_这个对话还没有内容。_');
    expect(empty).toContain('共 0 轮问答');
  });
});

describe('conversationExportFileName', () => {
  it('去掉文件名里的非法字符并附上日期', () => {
    expect(conversationExportFileName('a/b: c?', AT)).toBe('a b c-20260928.md');
  });

  it('没有标题时用占位标题', () => {
    expect(conversationExportFileName('  ', AT)).toBe('新对话-20260928.md');
  });
});
