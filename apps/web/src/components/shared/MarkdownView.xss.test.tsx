// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render, waitFor } from '@testing-library/react';
import { MarkdownViewImpl } from './MarkdownViewImpl';

/**
 * 资料内容（可能来自网页抓取、别人给的文档）和模型输出都会走这里渲染。
 * 任何一段能执行脚本的内容，都等于让资料作者在本机前端里跑代码——而前端能调本地 API。
 *
 * 必须真的挂载到 DOM 再检查：streamdown 在客户端异步渲染，renderToStaticMarkup
 * 只拿到一个空 div，断言会全部「通过」却什么都没验证。
 */
const PAYLOADS = [
  '<script>window.__pwned = 1</script>',
  '<img src=x onerror="window.__pwned=1">',
  '[点我](javascript:window.__pwned=1)',
  '<a href="javascript:alert(1)">链接</a>',
  '<iframe src="https://evil.example"></iframe>',
  '<svg onload="alert(1)"></svg>',
  '![图](javascript:alert(1))',
  '<div style="background:url(javascript:alert(1))">x</div>',
];

async function renderSettled(markdown: string): Promise<HTMLElement> {
  // 每段前后加一句正文，等它出现就说明渲染已完成
  const { container } = render(<MarkdownViewImpl>{`开头标记\n\n${markdown}\n\n结尾标记`}</MarkdownViewImpl>);
  await waitFor(() => expect(container.textContent).toContain('结尾标记'));
  return container;
}

afterEach(() => cleanup());

describe('MarkdownView 不执行资料里的脚本', () => {
  it('对照：正常 Markdown 照常渲染', async () => {
    const container = await renderSettled('**加粗** 与 [正常链接](https://example.com)');
    // streamdown 把 ** 渲染成带 data-streamdown="strong" 的 span
    expect(container.querySelector('[data-streamdown="strong"]')?.textContent).toBe('加粗');
    expect(container.innerHTML).toContain('https://example.com');
  });

  for (const payload of PAYLOADS) {
    it(payload, async () => {
      const container = await renderSettled(payload);
      const html = container.innerHTML;
      expect(container.querySelector('script, iframe')).toBeNull();
      expect(html).not.toMatch(/\son[a-z]+=/i);
      expect(html).not.toMatch(/javascript:/i);
      expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
    });
  }
});
