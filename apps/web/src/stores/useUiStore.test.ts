import { beforeEach, describe, expect, it } from 'vitest';

import { useUiStore } from './useUiStore';

describe('useUiStore 的证据定位', () => {
  beforeEach(() => {
    useUiStore.setState({ highlightedChunkId: null, highlightRequestSeq: 0 });
  });

  it('每次请求定位都让计数 +1', () => {
    const { setHighlightedChunkId } = useUiStore.getState();
    setHighlightedChunkId('chunk-a');
    expect(useUiStore.getState().highlightedChunkId).toBe('chunk-a');
    expect(useUiStore.getState().highlightRequestSeq).toBe(1);
  });

  it('连点同一条证据，id 不变但计数要变', () => {
    // 这正是「点了没反应」的成因：证据栏的滚动 effect 只盯着 id，
    // 同一条连点两次时 id 没变，effect 不重跑，用户点了「定位」什么也不发生。
    const { setHighlightedChunkId } = useUiStore.getState();
    setHighlightedChunkId('chunk-a');
    const first = useUiStore.getState().highlightRequestSeq;
    setHighlightedChunkId('chunk-a');
    const second = useUiStore.getState().highlightRequestSeq;

    expect(useUiStore.getState().highlightedChunkId).toBe('chunk-a');
    expect(second).toBeGreaterThan(first);
  });

  it('清空高亮同样算一次请求', () => {
    const { setHighlightedChunkId } = useUiStore.getState();
    setHighlightedChunkId(null);
    expect(useUiStore.getState().highlightedChunkId).toBeNull();
    expect(useUiStore.getState().highlightRequestSeq).toBe(1);
  });
});

describe('useUiStore 的阅读器目标', () => {
  it('开与关只动 activeReaderTarget', () => {
    const { openReader, closeReader } = useUiStore.getState();
    openReader({ documentId: 'doc-1', documentTitle: '文档', chunkId: 'chunk-9' });
    expect(useUiStore.getState().activeReaderTarget).toEqual({
      documentId: 'doc-1',
      documentTitle: '文档',
      chunkId: 'chunk-9',
    });
    closeReader();
    expect(useUiStore.getState().activeReaderTarget).toBeNull();
  });
});
