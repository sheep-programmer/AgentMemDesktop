import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { ContextUsageSummary } from './ContextUsageSummary';

afterEach(cleanup);

it('labels input estimates separately from billing and compares with the same-turn baseline', () => {
  render(
    <ContextUsageSummary
      usage={{
        mode: 'economy',
        original_estimated_tokens: 2000,
        estimated_tokens: 1200,
        saved_estimated_tokens: 800,
        history_messages: 2,
        evidence_count: 3,
        insight_count: 1,
        card_count: 1,
      }}
    />,
  );
  const summary = screen.getByRole('status');
  expect(summary.textContent).toContain('节省上下文');
  expect(summary.textContent).toContain('1,200');
  expect(summary.textContent).toContain('800（40%）');
  expect(summary.textContent).toContain('估算值，非计费量');
});
