import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { FourStateView } from './FourStateView';

afterEach(cleanup);
it('announces loading and exposes content when it becomes ready', () => {
  const { rerender } = render(
    <FourStateView status="loading">
      <p>实际资料</p>
    </FourStateView>,
  );
  expect(screen.getByRole('status').textContent).toContain('正在加载');
  expect(screen.queryByText('实际资料')).toBeNull();
  rerender(
    <FourStateView status="ready">
      <p>实际资料</p>
    </FourStateView>,
  );
  expect(screen.queryByRole('status')).toBeNull();
  expect(screen.getByText('实际资料')).toBeTruthy();
});
it('keeps a retry action alongside the announced failure', () => {
  const retry = vi.fn();
  render(
    <FourStateView status="error" error="服务暂时离线" onRetry={retry}>
      {null}
    </FourStateView>,
  );
  expect(screen.getByRole('alert').textContent).toContain('服务暂时离线');
  fireEvent.click(screen.getByRole('button', { name: '重新加载' }));
  expect(retry).toHaveBeenCalledOnce();
});
