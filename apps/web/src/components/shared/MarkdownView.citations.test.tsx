// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render, waitFor } from '@testing-library/react';
import { MarkdownViewImpl } from './MarkdownViewImpl';
import type { CitationMarker } from '@/lib/api/types.temp';

/**
 * 回归：streamdown 按内容分块 memo，引用事件晚到时先渲染的块被缓存住，
 * 同一条回答里出现「前面的标记是灰色 [cN]、后面的是数字芯片」的混排。
 * 修复后 citations 变化会重挂载，所有标记必须统一渲染成数字芯片。
 */
const CITATIONS: CitationMarker[] = [
  {
    marker: 'c1',
    chunk_id: 'chk-1',
    document_id: 'doc-1',
    document_title: '甲文档',
    snippet: '片段甲',
  },
  {
    marker: 'c2',
    chunk_id: 'chk-2',
    document_id: 'doc-2',
    document_title: '乙文档',
    snippet: '片段乙',
  },
];

afterEach(() => cleanup());

describe('引用标记渲染', () => {
  it('citations 到达后所有标记统一成数字芯片，不再残留灰色兜底', async () => {
    const content = '第一句结论 [^c1]\n\n第二句结论 [^c2]\n\n结尾标记';
    const { container, rerender } = render(
      <MarkdownViewImpl>{content}</MarkdownViewImpl>,
    );
    await waitFor(() => expect(container.textContent).toContain('结尾标记'));
    // 引用还没到达：如实显示模型写下的标记，不假装能点（既有设计）
    expect(container.textContent).toContain('[c1]');

    rerender(
      <MarkdownViewImpl citations={CITATIONS}>{content}</MarkdownViewImpl>,
    );
    await waitFor(() => {
      expect(container.textContent).not.toContain('[c1]');
      expect(container.textContent).not.toContain('[c2]');
    });
    const chips = [...container.querySelectorAll('[data-slot="tooltip-trigger"]')];
    expect(chips.map((chip) => chip.textContent)).toEqual(['1', '2']);
  });

  it('匹配不到真实引用的标记保持灰色原文，不渲染成死链芯片', async () => {
    const content = '这一句引用了不存在的编号 [^c9]\n\n结尾标记';
    const { container } = render(
      <MarkdownViewImpl citations={CITATIONS}>{content}</MarkdownViewImpl>,
    );
    await waitFor(() => expect(container.textContent).toContain('结尾标记'));
    expect(container.textContent).toContain('[c9]');
    expect(
      container.querySelector('[data-slot="tooltip-trigger"]'),
    ).toBeNull();
  });
});
