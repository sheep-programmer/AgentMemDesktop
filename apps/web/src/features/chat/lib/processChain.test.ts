import { describe, expect, it } from 'vitest';
import type { ProcessStage } from '@/lib/api/types.temp';
import { CHAIN_PLACEHOLDER, buildSummaryChain, isChainNotStarted } from './processChain';

/** 复刻 useChatStream 的初始阶段表——四个阶段、id 与 label 都照抄。 */
function freshStages(): ProcessStage[] {
  return [
    { id: 'rewrite', label: '查询改写', status: 'pending' },
    { id: 'retrieval', label: '混合检索', status: 'pending' },
    { id: 'insights', label: '经验注入', status: 'pending' },
    { id: 'generating', label: '模型生成', status: 'pending' },
  ];
}

describe('buildSummaryChain', () => {
  it('四个阶段都在链条里——id 写错时前两环会整段消失，这是回归的重点', () => {
    const chain = buildSummaryChain(freshStages(), false);
    expect(chain.split(' → ')).toHaveLength(4);
    expect(chain).toContain('意图解析');
    expect(chain).toContain('知识检索');
  });

  it('一次提问都没发生时，不许说任何一环已完成', () => {
    const chain = buildSummaryChain(freshStages(), false);
    expect(chain).not.toContain('完成');
    expect(chain).toBe('⟳ 意图解析 → 🔍 知识检索 → 🧩 经验注入 → ✍️ 推理生成');
  });

  it('未跑到的阶段不因为 isStreaming=false 就变成完成态', () => {
    const stages = freshStages();
    stages[0] = { ...stages[0], status: 'done' };
    stages[1] = { ...stages[1], status: 'running' };
    const chain = buildSummaryChain(stages, false);
    expect(chain).toContain('⟳ 改写查询');
    expect(chain).toContain('🔍 检索文献中');
    // 后两环还没跑，措辞必须是预告而不是完成
    expect(chain).toContain('🧩 经验注入');
    expect(chain).toContain('✍️ 推理生成');
    expect(chain).not.toContain('推理生成完成');
  });

  it('条数只从 detail 里抠，抠不到就不提条数', () => {
    const withCount: ProcessStage[] = [
      { id: 'retrieval', label: '混合检索', status: 'done', detail: '召回 6 条切片证据' },
      { id: 'insights', label: '经验注入', status: 'done', detail: '注入 2 条置信经验' },
    ];
    expect(buildSummaryChain(withCount, false)).toBe('🔍 检索到 6 条资料 → 🧩 应用 2 条经验');

    const withoutCount: ProcessStage[] = [
      { id: 'retrieval', label: '混合检索', status: 'done' },
      { id: 'insights', label: '经验注入', status: 'done' },
    ];
    expect(buildSummaryChain(withoutCount, false)).toBe('🔍 检索完成 → 🧩 经验匹配完成');
  });

  it('流式中：生成环说正在生成', () => {
    const stages = freshStages().map((stage) =>
      stage.id === 'generating' ? { ...stage, status: 'running' as const } : { ...stage, status: 'done' as const },
    );
    expect(buildSummaryChain(stages, true)).toContain('✍️ 正在生成深度解答');
  });

  it('全部跑完：生成环说完成', () => {
    const stages = freshStages().map((stage) => ({ ...stage, status: 'done' as const }));
    expect(buildSummaryChain(stages, false)).toContain('✍️ 推理生成完成');
  });

  it('拿不到任何阶段时退回占位链条', () => {
    expect(buildSummaryChain([], false)).toBe(CHAIN_PLACEHOLDER);
  });
});

describe('isChainNotStarted', () => {
  it('全 pending 与空数组都算没开始', () => {
    expect(isChainNotStarted(freshStages())).toBe(true);
    expect(isChainNotStarted([])).toBe(true);
  });

  it('只要有一环动过就算开始了', () => {
    const stages = freshStages();
    stages[0] = { ...stages[0], status: 'running' };
    expect(isChainNotStarted(stages)).toBe(false);
  });
});

describe('检索降级提示', () => {
  it('检索阶段带 warning 时，摘要行要写明降级', () => {
    const stages: ProcessStage[] = [
      { id: 'retrieval', label: '混合检索', status: 'done', detail: '召回 8 条切片证据', warning: '向量检索没用上' },
    ];
    expect(buildSummaryChain(stages, false)).toBe('🔍 检索到 8 条资料（⚠️ 检索降级）');
  });

  it('没降级时摘要行不变', () => {
    const stages: ProcessStage[] = [
      { id: 'retrieval', label: '混合检索', status: 'done', detail: '召回 8 条切片证据' },
    ];
    expect(buildSummaryChain(stages, false)).toBe('🔍 检索到 8 条资料');
  });
});

describe('describeDegraded', () => {
  it('把降级环节翻成人话，空列表返回 undefined', async () => {
    const { describeDegraded } = await import('../hooks/useChatStream');
    expect(describeDegraded([])).toBeUndefined();
    expect(describeDegraded(undefined)).toBeUndefined();
    expect(describeDegraded(['vector'])).toContain('只按关键词匹配');
    expect(describeDegraded(['vector', 'rerank'])).toContain('粗排顺序');
  });
});
