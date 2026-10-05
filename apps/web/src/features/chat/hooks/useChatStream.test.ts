import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { chatService, type ChatStreamHandlers } from '@/lib/api/services/chat';
import { isMockMode } from '@/lib/api/client';
import { mockTraceDetail } from '@/lib/api/mock/data';
import { toast } from 'sonner';
import { useChatStream } from './useChatStream';

vi.mock('@/lib/api/services/chat', () => ({
  chatService: {
    streamChat: vi.fn(),
    stopChat: vi.fn(),
    getConversation: vi.fn(),
    getTrace: vi.fn(),
  },
}));
vi.mock('@/lib/api/client', () => ({ isMockMode: vi.fn(() => false) }));
vi.mock('sonner', () => ({ toast: { error: vi.fn(), info: vi.fn() } }));

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}
const requests: Array<{
  handlers: ChatStreamHandlers;
  signal: AbortSignal;
  done: ReturnType<typeof deferred<void>>;
}> = [];
beforeEach(() => {
  vi.clearAllMocks();
  requests.length = 0;
  vi.mocked(isMockMode).mockReturnValue(false);
  vi.mocked(chatService.streamChat).mockImplementation(
    (_id, _text, handlers, signal) => {
      const done = deferred<void>();
      requests.push({ handlers, signal: signal!, done });
      return done.promise;
    },
  );
  vi.mocked(chatService.stopChat).mockResolvedValue({ stopped: true });
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('chat stream lifecycle', () => {
  it('passes the context mode and discards cost estimates from obsolete requests', async () => {
    const { result } = renderHook(() =>
      useChatStream({ spaceId: 'S', conversationId: 'A' }),
    );
    const usage = {
      mode: 'economy' as const,
      original_estimated_tokens: 2000,
      estimated_tokens: 1200,
      saved_estimated_tokens: 800,
      history_messages: 2,
      evidence_count: 3,
      insight_count: 1,
      card_count: 1,
    };
    act(() => {
      void result.current.sendMessage('问题', { contextMode: 'economy' });
      requests[0].handlers.onContext?.(usage);
    });
    expect(
      vi.mocked(chatService.streamChat).mock.calls[0][4]?.contextMode,
    ).toBe('economy');
    expect(result.current.contextUsage).toEqual(usage);
    act(() => result.current.resetConversation());
    act(() => requests[0].handlers.onContext?.(usage));
    expect(result.current.contextUsage).toBeNull();
    await act(async () => requests[0].done.resolve());
  });
  it('starts only one request when send is triggered twice in one render', async () => {
    const { result } = renderHook(() =>
      useChatStream({ spaceId: 'S', conversationId: 'A' }),
    );
    act(() => {
      void result.current.sendMessage('第一问');
      void result.current.sendMessage('第二问');
    });
    expect(chatService.streamChat).toHaveBeenCalledOnce();
    expect(result.current.messages).toHaveLength(2);
    await act(async () => {
      requests[0].handlers.onDone?.({ message_id: 'm', trace_id: 't' });
      requests[0].done.resolve();
    });
    expect(result.current.isStreaming).toBe(false);
  });

  it('old errors and callbacks cannot stop or overwrite a new response', async () => {
    const { result } = renderHook(() =>
      useChatStream({ spaceId: 'S', conversationId: 'A' }),
    );
    act(() => {
      void result.current.sendMessage('旧问题');
    });
    act(() => result.current.resetConversation());
    act(() => {
      void result.current.sendMessage('新问题');
      requests[1].handlers.onTraceStart?.({ trace_id: 'new' });
    });
    await act(async () => {
      requests[0].handlers.onTraceStart?.({ trace_id: 'old' });
      requests[0].handlers.onDelta?.({ text: '旧内容' });
      requests[0].handlers.onError?.({ message: '旧错误' });
      requests[0].done.reject(new Error('old transport failed'));
    });
    expect(result.current.isStreaming).toBe(true);
    expect(result.current.activeTrace?.id).toBe('new');
    expect(result.current.messages[0].content).toBe('新问题');
    expect(toast.error).not.toHaveBeenCalled();
  });

  it('an old stop response cannot reset the next generation', async () => {
    const stopped = deferred<{ stopped: boolean }>();
    vi.mocked(chatService.stopChat).mockReturnValue(stopped.promise);
    const { result } = renderHook(() =>
      useChatStream({ spaceId: 'S', conversationId: 'A' }),
    );
    act(() => {
      void result.current.sendMessage('一', {
        conversationId: 'actual-conversation',
      });
    });
    act(() => {
      void result.current.abortStream();
    });
    expect(chatService.stopChat).toHaveBeenCalledWith('actual-conversation');
    act(() => {
      void result.current.sendMessage('二');
    });
    await act(async () => stopped.resolve({ stopped: true }));
    expect(result.current.isStreaming).toBe(true);
    expect(result.current.processStages[0].status).toBe('running');
  });

  it('a stream without a terminal event ends with a visible incomplete answer', async () => {
    const { result } = renderHook(() =>
      useChatStream({ spaceId: 'S', conversationId: 'A' }),
    );
    act(() => {
      void result.current.sendMessage('问题');
      requests[0].handlers.onDelta?.({ text: '已有内容' });
    });
    await act(async () => requests[0].done.resolve());
    expect(result.current.isStreaming).toBe(false);
    expect(result.current.messages[1].content).toContain('已有内容');
    expect(result.current.messages[1].content).toContain('回答未完成');
  });

  it('merges a burst of deltas into one state update', async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() =>
      useChatStream({ spaceId: 'S', conversationId: 'A' }),
    );
    act(() => {
      void result.current.sendMessage('问题');
    });
    act(() => {
      requests[0].handlers.onDelta?.({ text: '一' });
      requests[0].handlers.onDelta?.({ text: '二' });
      requests[0].handlers.onDelta?.({ text: '三' });
    });
    // 还在同一个时间片里：增量先缓冲，不是每个 token 都触发一次整树重渲染
    expect(result.current.messages[1].content).toBe('');
    await act(async () => {
      vi.advanceTimersByTime(60);
    });
    expect(result.current.messages[1].content).toBe('一二三');
    await act(async () => {
      requests[0].handlers.onDone?.({ message_id: 'm', trace_id: 't' });
      requests[0].done.resolve();
    });
    expect(result.current.messages[1].content).toBe('一二三');
  });

  it('manual abort keeps the buffered tail of the answer', async () => {
    const { result } = renderHook(() =>
      useChatStream({ spaceId: 'S', conversationId: 'A' }),
    );
    act(() => {
      void result.current.sendMessage('问题');
    });
    act(() => {
      requests[0].handlers.onDelta?.({ text: '写到一半的内容' });
    });
    act(() => {
      void result.current.abortStream();
    });
    // 终止时缓冲里的最后一段要先落地，不能随取消一起丢掉
    expect(result.current.messages[1].content).toBe('写到一半的内容');
    await act(async () => requests[0].done.reject(new Error('aborted')));
  });

  it('citation events keep char_offset so inline chips render during streaming', async () => {
    const { result } = renderHook(() =>
      useChatStream({ spaceId: 'S', conversationId: 'A' }),
    );
    act(() => {
      void result.current.sendMessage('问题');
    });
    act(() => {
      requests[0].handlers.onCitation?.({
        marker: 'c1',
        chunk_id: 'chk-1',
        document_id: 'doc-1',
        document_title: '甲文档',
        snippet: '片段',
        char_offset: 12,
      });
    });
    // char_offset 被丢掉的话，inline 引用芯片要等刷新读历史才出现
    expect(result.current.messages[1].citations?.[0]?.char_offset).toBe(12);
    await act(async () => {
      requests[0].handlers.onDone?.({ message_id: 'm', trace_id: 't' });
      requests[0].done.resolve();
    });
  });

  it('trace requests from a previous conversation are discarded', async () => {
    const trace = deferred<typeof mockTraceDetail>();
    vi.mocked(chatService.getTrace).mockReturnValue(trace.promise);
    const { result } = renderHook(() =>
      useChatStream({ spaceId: 'S', conversationId: 'A' }),
    );
    let focused!: Promise<boolean>;
    act(() => {
      focused = result.current.focusTrace('old-trace');
    });
    act(() => result.current.resetConversation());
    await act(async () => trace.resolve(mockTraceDetail));
    expect(await focused).toBe(false);
    expect(result.current.activeTrace).toBeNull();
  });

  it('unmount aborts the stream and ignores subsequent failures', () => {
    const { result, unmount } = renderHook(() =>
      useChatStream({ spaceId: 'S', conversationId: 'A' }),
    );
    act(() => {
      void result.current.sendMessage('问题');
    });
    unmount();
    expect(requests[0].signal.aborted).toBe(true);
    requests[0].handlers.onError?.({ message: 'late error' });
    expect(toast.error).not.toHaveBeenCalled();
  });

  it('mock cancellation clears pending timers and never publishes old progress', async () => {
    vi.useFakeTimers();
    vi.mocked(isMockMode).mockReturnValue(true);
    const { result } = renderHook(() =>
      useChatStream({ spaceId: 'S', conversationId: 'A' }),
    );
    let completion!: Promise<void>;
    act(() => {
      completion = result.current.sendMessage('问题');
    });
    act(() => result.current.resetConversation());
    await act(async () => completion);
    expect(vi.getTimerCount()).toBe(0);
    expect(result.current.messages).toEqual([]);
    expect(result.current.activeTrace).toBeNull();
    expect(
      result.current.processStages.every((stage) => stage.status === 'pending'),
    ).toBe(true);
  });
});
