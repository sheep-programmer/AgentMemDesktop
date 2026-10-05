import { describe, expect, it } from 'vitest';
import type { ProviderHealth } from '@/lib/api/types.temp';
import {
  boundProviderIds,
  checkProviders,
  toCheckState,
  type ProviderCheckState,
} from './checkBoundProviders';

describe('boundProviderIds', () => {
  it('多个角色绑同一个服务商时只测一次', () => {
    const ids = boundProviderIds(
      { chat: 'deepseek', fast: 'deepseek', distill: 'deepseek', embedding: 'bge' },
      ['deepseek', 'bge', 'unused'],
    );
    expect(ids).toEqual(['deepseek', 'bge']);
  });

  it('跳过未绑定的角色和已不存在的服务商', () => {
    const ids = boundProviderIds({ chat: 'gone', fast: null, rerank: 'bge-rerank' }, ['bge-rerank']);
    expect(ids).toEqual(['bge-rerank']);
  });
});

describe('toCheckState', () => {
  it('成功时带上延迟', () => {
    expect(toCheckState({ status: 'fulfilled', value: { ok: true, latency_ms: 42 } })).toEqual({
      status: 'online',
      latencyMs: 42,
    });
  });

  it('后端返回失败时带上原因，缺原因时给默认文案', () => {
    expect(toCheckState({ status: 'fulfilled', value: { ok: false, error: '401 Unauthorized' } })).toEqual({
      status: 'failed',
      error: '401 Unauthorized',
    });
    expect(toCheckState({ status: 'fulfilled', value: { ok: false } })).toEqual({
      status: 'failed',
      error: '无法建立连接',
    });
  });

  it('请求本身抛异常也算失败', () => {
    expect(toCheckState({ status: 'rejected', reason: new Error('网络断开') })).toEqual({
      status: 'failed',
      error: '网络断开',
    });
  });
});

describe('checkProviders', () => {
  it('并行发出，每个结果单独回调，某个抛错不影响其他', async () => {
    const started: string[] = [];
    const results: Record<string, ProviderCheckState> = {};
    const check = (id: string): Promise<ProviderHealth> => {
      started.push(id);
      if (id === 'bad') return Promise.reject(new Error('超时'));
      return Promise.resolve({ ok: true, latency_ms: 10 });
    };

    const done = checkProviders(['a', 'bad', 'b'], check, (id, state) => {
      results[id] = state;
    });
    // 三个请求在第一个结果回来之前就都已发出
    expect(started).toEqual(['a', 'bad', 'b']);
    await done;

    expect(results).toEqual({
      a: { status: 'online', latencyMs: 10 },
      bad: { status: 'failed', error: '超时' },
      b: { status: 'online', latencyMs: 10 },
    });
  });
});
