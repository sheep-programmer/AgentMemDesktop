import { expect, it, vi } from 'vitest';
import { fetchSSE } from '../sse';
import { chatService } from './chat';

vi.mock('./spaces', () => ({ isMockMode: () => false }));
vi.mock('../sse', () => ({ fetchSSE: vi.fn().mockResolvedValue(undefined) }));

it('sends economy mode to the API and delivers the context estimate event', async () => {
  const onContext = vi.fn();
  await chatService.streamChat('c', '问题', { onContext }, undefined, {
    contextMode: 'economy',
  });
  const options = vi.mocked(fetchSSE).mock.calls.at(-1)![0];
  expect(options.body).toMatchObject({
    context_mode: 'economy',
    content: '问题',
  });
  const usage = {
    mode: 'economy',
    original_estimated_tokens: 2000,
    estimated_tokens: 1200,
    saved_estimated_tokens: 800,
    history_messages: 2,
    evidence_count: 3,
    insight_count: 1,
    card_count: 1,
  };
  options.handlers?.context(usage);
  expect(onContext).toHaveBeenCalledWith(usage);
});

it('keeps the standard context default for existing callers', async () => {
  await chatService.streamChat('c', '问题', {});
  expect(vi.mocked(fetchSSE).mock.calls.at(-1)![0].body).toMatchObject({
    context_mode: 'standard',
  });
});
