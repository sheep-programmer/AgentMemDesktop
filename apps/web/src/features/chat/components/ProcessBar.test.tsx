import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { ProcessBar } from './ProcessBar';
import type { ProcessStage } from '@/lib/api/types';

afterEach(cleanup);

it('shows the active step without automatically expanding or moving the answer', () => {
  const stages: ProcessStage[] = [
    { id: 'rewrite', label: '查询改写', status: 'done' },
    {
      id: 'retrieval',
      label: '混合检索',
      status: 'running',
      detail: '查找相关资料',
    },
    { id: 'generating', label: '模型生成', status: 'pending' },
  ];
  render(<ProcessBar stages={stages} isStreaming />);
  expect(screen.getByRole('status').textContent).toBe('正在查找相关资料');
  const trigger = screen.getByRole('button', { name: '查看回答生成过程' });
  expect(trigger.getAttribute('aria-expanded')).toBe('false');
  fireEvent.click(trigger);
  expect(trigger.getAttribute('aria-expanded')).toBe('true');
  expect(
    document.getElementById(trigger.getAttribute('aria-controls')!),
  ).toBeTruthy();
  expect(screen.getByText('查找相关资料')).toBeTruthy();
});

it('distinguishes a stopped answer from successful completion', () => {
  render(
    <ProcessBar
      stages={[
        {
          id: 'generating',
          label: '模型生成',
          status: 'failed',
          detail: '用户主动中止',
        },
      ]}
      isStreaming={false}
    />,
  );
  expect(screen.getByRole('status').textContent).toBe('回答已停止');
  expect(screen.queryByText(/回答已完成/)).toBeNull();
});

it('keeps restricted retrieval visible after the details collapse', () => {
  render(
    <ProcessBar
      stages={[
        {
          id: 'retrieval',
          label: '混合检索',
          status: 'done',
          detail: '召回 3 条切片证据',
          warning: '向量检索不可用',
        },
        { id: 'generating', label: '模型生成', status: 'done' },
      ]}
      isStreaming={false}
    />,
  );
  expect(screen.getByRole('status').textContent).toBe(
    '回答已完成 · 找到 3 条资料 · 检索受限',
  );
});

it('shows recorded history as a record instead of waiting steps', () => {
  render(
    <ProcessBar
      stages={[{ id: 'generating', label: '模型生成', status: 'pending' }]}
      isStreaming={false}
      meta={{ model: 'recorded-model', tokens: 150, latencyMs: 2000 }}
    />,
  );
  expect(screen.getByRole('status').textContent).toContain('生成记录');
  fireEvent.click(screen.getByRole('button', { name: '查看回答生成过程' }));
  expect(screen.getByText('总用量：150 token')).toBeTruthy();
  expect(screen.queryByText('等待中')).toBeNull();
});
