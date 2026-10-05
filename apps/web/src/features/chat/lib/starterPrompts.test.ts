import { describe, expect, it } from 'vitest';
import type { DocumentItem } from '@/lib/api/types.temp';
import { buildStarterPrompts } from './starterPrompts';

function doc(title: string, status = 'ready', summary?: string): DocumentItem {
  return { id: title, title, status, meta: { context_summary: summary } } as unknown as DocumentItem;
}

describe('buildStarterPrompts', () => {
  it('没有资料就不推荐，不拿写死的示例顶上', () => {
    expect(buildStarterPrompts([])).toEqual([]);
  });

  it('只取已就绪的文档，最多三条，标题去掉扩展名', () => {
    const prompts = buildStarterPrompts([
      doc('解析中.md', 'parsing'),
      doc('稳定性数据.md'),
      doc('长江大学建立时间', 'ready', '长江大学是一所位于湖北省荆州市的综合性公办本科高校'),
      doc('c.pdf'),
      doc('d.md'),
    ]);
    expect(prompts.map((p) => p.title)).toEqual(['稳定性数据', '长江大学建立时间', 'c']);
    expect(prompts[0].query).toBe('《稳定性数据》主要讲了什么？有哪些要点？');
    expect(prompts[1].desc).toContain('荆州市');
    expect(prompts[0].desc).toBe('知识库中的《稳定性数据》');
  });
});
