import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { describe, expect, it } from 'vitest';
import { ConfirmProvider, useConfirm } from './ConfirmProvider';

function Probe() {
  const confirm = useConfirm();
  return (
    <>
      <button
        type="button"
        onClick={async () => {
          const accepted = await confirm({
            title: '删除资料？',
            description: '资料会被移除。',
            confirmText: '删除',
            destructive: true,
          });
          const result = document.querySelector('[data-confirm-result]')!;
          result.setAttribute('aria-label', '确认结果');
          result.setAttribute('role', 'status');
          result.textContent = String(accepted);
        }}
      >
        打开确认
      </button>
      <output data-confirm-result />
    </>
  );
}

describe('ConfirmProvider', () => {
  it('uses an accessible themed dialog and resolves both decisions once', async () => {
    render(
      <MemoryRouter>
        <ConfirmProvider>
          <Probe />
        </ConfirmProvider>
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole('button', { name: '打开确认' }));
    expect(screen.getByRole('alertdialog')).toBeTruthy();
    expect(screen.getByRole('heading', { name: '删除资料？' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    expect(screen.queryByRole('alertdialog')).toBeNull();
    await waitFor(() =>
      expect(screen.getByRole('status', { name: '确认结果' }).textContent).toBe('false'),
    );

    fireEvent.click(screen.getByRole('button', { name: '打开确认' }));
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '删除' }));
    });
    await waitFor(() =>
      expect(screen.getByRole('status', { name: '确认结果' }).textContent).toBe('true'),
    );
  });
});
