import { describe, expect, it } from 'vitest';
import { deriveConversationTitle } from './conversationTitle';

describe('deriveConversationTitle', () => {
  it('短提问原样成为标题', () => {
    expect(deriveConversationTitle('hERG 的判定阈值是多少？')).toBe('hERG 的判定阈值是多少？');
  });

  it('长提问截断并加省略号', () => {
    const title = deriveConversationTitle(
      '针对 EGFR T790M 耐药突变有哪些小分子先导化合物结构优化策略？',
    );
    expect(title).toBe('针对 EGFR T790M 耐药突变有哪…');
    expect(title!.length).toBe(21); // 20 字 + 省略号
  });

  it('只取第一行，跳过空行', () => {
    expect(deriveConversationTitle('\n\n第一行问题\n第二行补充说明')).toBe('第一行问题');
  });

  it('剥掉行首的标题号与列表符号', () => {
    expect(deriveConversationTitle('## 这是标题')).toBe('这是标题');
    expect(deriveConversationTitle('- 列表项提问')).toBe('列表项提问');
    expect(deriveConversationTitle('> 引用的问题')).toBe('引用的问题');
  });

  it('剥掉行内 markdown 标记但保留文字', () => {
    expect(deriveConversationTitle('**加粗**的`代码`问题')).toBe('加粗的代码问题');
  });

  it('跳过代码块围栏，用围栏后的内容', () => {
    expect(deriveConversationTitle('```python\nprint(1)\n```')).toBe('print(1)');
  });

  it('多个空白折叠成一个空格', () => {
    expect(deriveConversationTitle('这里    有   很多空格')).toBe('这里 有 很多空格');
  });

  it('取不出可读内容时返回 null，交由调用方保持占位标题', () => {
    expect(deriveConversationTitle('')).toBeNull();
    expect(deriveConversationTitle('   \n\n  ')).toBeNull();
    expect(deriveConversationTitle('***')).toBeNull();
    expect(deriveConversationTitle('```')).toBeNull();
  });
});
