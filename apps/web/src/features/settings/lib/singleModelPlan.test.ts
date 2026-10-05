import { describe, expect, it } from 'vitest';
import { applyChanges, LLM_ROLES, planSingleModel, sharedLlmProvider } from './singleModelPlan';

describe('planSingleModel', () => {
  it('只改四个 LLM 角色，向量和重排不动', () => {
    const changes = planSingleModel(
      { chat: 'a', fast: 'b', distill: 'c', judge: 'd', embedding: 'bge', rerank: 'bge-rerank' },
      'deepseek',
    );
    expect(changes.map((c) => c.role)).toEqual(['chat', 'fast', 'distill', 'judge']);
    expect(changes.every((c) => c.to === 'deepseek')).toBe(true);
  });

  it('已经绑在它身上的角色不重复改', () => {
    const changes = planSingleModel({ chat: 'deepseek', fast: 'qwen', distill: 'deepseek', judge: 'gpt' }, 'deepseek');
    expect(changes).toEqual([
      { role: 'fast', from: 'qwen', to: 'deepseek' },
      { role: 'judge', from: 'gpt', to: 'deepseek' },
    ]);
  });

  it('未绑定的角色记为从 null 改过来', () => {
    const changes = planSingleModel({ chat: 'deepseek', fast: null }, 'deepseek');
    expect(changes).toEqual([
      { role: 'fast', from: null, to: 'deepseek' },
      { role: 'distill', from: null, to: 'deepseek' },
      { role: 'judge', from: null, to: 'deepseek' },
    ]);
  });

  it('四个角色都已是它时无需改动', () => {
    const bindings = Object.fromEntries(LLM_ROLES.map((role) => [role, 'deepseek']));
    expect(planSingleModel({ ...bindings, embedding: 'bge' }, 'deepseek')).toEqual([]);
  });
});

describe('applyChanges', () => {
  it('合并成完整绑定一次提交，保留向量和重排', () => {
    const bindings = { chat: 'a', fast: 'deepseek', distill: null, judge: 'd', embedding: 'bge', rerank: 'bge-rerank' };
    const next = applyChanges(bindings, planSingleModel(bindings, 'deepseek'));
    expect(next).toEqual({
      chat: 'deepseek',
      fast: 'deepseek',
      distill: 'deepseek',
      judge: 'deepseek',
      embedding: 'bge',
      rerank: 'bge-rerank',
    });
    // 不改传入的对象，失败时还要拿它回滚
    expect(bindings.chat).toBe('a');
  });
});

describe('sharedLlmProvider', () => {
  it('四个角色一致时返回该服务商', () => {
    expect(sharedLlmProvider({ chat: 'x', fast: 'x', distill: 'x', judge: 'x', embedding: 'bge' })).toBe('x');
  });

  it('不一致或有未绑定时返回 null', () => {
    expect(sharedLlmProvider({ chat: 'x', fast: 'y', distill: 'x', judge: 'x' })).toBeNull();
    expect(sharedLlmProvider({ chat: 'x', fast: 'x', distill: 'x' })).toBeNull();
    expect(sharedLlmProvider({})).toBeNull();
  });
});
