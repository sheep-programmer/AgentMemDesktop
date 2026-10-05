import { beforeEach, describe, expect, it } from 'vitest';

import { HL_ATTR, clearHighlights, highlightBlocks, plainText } from './highlight';

/** 造一棵「渲染好的 Markdown」DOM。 */
function render(html: string): HTMLElement {
  const root = document.createElement('div');
  root.innerHTML = html;
  document.body.appendChild(root);
  return root;
}

const marked = (root: HTMLElement) =>
  Array.from(root.querySelectorAll(`[${HL_ATTR}]`)).map((el) => el.textContent?.trim());

beforeEach(() => {
  document.body.innerHTML = '';
});

describe('plainText', () => {
  it('只留文字与数字：渲染之后 Markdown 记号已经不存在了', () => {
    expect(plainText('## 标题 —— 第 3 节')).toBe('标题第3节');
    expect(plainText('a-b_c 1.2')).toBe('abc12');
  });
});

describe('highlightBlocks', () => {
  it('把首尾探测片段之间的块整段标上', () => {
    const root = render('<p>第一段落文字</p><p>第二段落文字</p><p>第三段落文字</p>');
    const anchor = highlightBlocks(root, '第一段落文字', '第二段落文字');

    expect(anchor?.textContent).toBe('第一段落文字');
    expect(marked(root)).toEqual(['第一段落文字', '第二段落文字']);
  });

  it('首尾落在同一块时只标那一块', () => {
    const root = render('<p>唯一的一段文字</p><p>另一段</p>');
    highlightBlocks(root, '唯一的一段文字', '唯一的一段文字');
    expect(marked(root)).toEqual(['唯一的一段文字']);
  });

  it('尾片段找不到时退化成只标首块，而不是整篇标满', () => {
    const root = render('<p>开头这一段</p><p>中间这一段</p><p>结尾这一段</p>');
    highlightBlocks(root, '开头这一段', '压根不存在的文字');
    expect(marked(root)).toEqual(['开头这一段']);
  });

  it('首片段找不到时返回 null，一个块都不标', () => {
    const root = render('<p>正文</p>');
    expect(highlightBlocks(root, '查无此段落', '也没有')).toBeNull();
    expect(marked(root)).toEqual([]);
  });

  it('表格按行为单位：切片从表格中间开始也能对上', () => {
    const root = render(
      '<table><tbody>' +
        '<tr><td>化合物</td><td>活性</td></tr>' +
        '<tr><td>CMPD-043</td><td>4.8 nM</td></tr>' +
        '<tr><td>CMPD-044</td><td>9.1 nM</td></tr>' +
        '</tbody></table>',
    );
    highlightBlocks(root, 'CMPD-043', 'CMPD-044');

    const rows = Array.from(root.querySelectorAll(`tr[${HL_ATTR}]`));
    expect(rows).toHaveLength(2);
    // 表格行不加左侧竖条，那会把表格边框顶坏
    expect(rows[0].className).not.toContain('border-l-2');
  });

  it('段落加左侧竖条，首尾各加一条横边', () => {
    const root = render('<p>开头这一段</p><p>结尾这一段</p>');
    highlightBlocks(root, '开头这一段', '结尾这一段');
    const [first, last] = Array.from(root.querySelectorAll(`[${HL_ATTR}]`));
    expect(first.className).toContain('border-l-2');
    expect(first.className).toContain('border-t-2');
    expect(last.className).toContain('border-b-2');
  });

  it('探测片段短于 4 个字时不硬猜：宁可不高亮，也不能标错段', () => {
    // 4 是 n-gram 的下限，再短就会撞上「的」「是」这类高频片段
    const root = render('<p>甲乙</p><p>另一段落文字</p>');
    expect(highlightBlocks(root, '甲乙', '甲乙')).toBeNull();
    expect(marked(root)).toEqual([]);
  });

  it('代码块内部的行不单独成块，整块 pre 作为一个单位', () => {
    const root = render('<pre><code>print(1)\nprint(2)</code></pre><p>后面一段</p>');
    highlightBlocks(root, 'print(1)', 'print(2)');
    expect(marked(root)).toEqual(['print(1)\nprint(2)']);
  });
});

describe('clearHighlights', () => {
  it('清干净标记与全部类名，重复执行不会越描越深', () => {
    const root = render('<p>某一段文字</p>');
    const before = root.querySelector('p')!.className;

    highlightBlocks(root, '某一段文字', '某一段文字');
    expect(marked(root)).toHaveLength(1);

    clearHighlights(root);
    clearHighlights(root);
    expect(marked(root)).toEqual([]);
    expect(root.querySelector('p')!.className).toBe(before);
  });

  it('清完之后可以重新标到另一段上', () => {
    const root = render('<p>甲段落内容</p><p>乙段落内容</p>');
    highlightBlocks(root, '甲段落内容', '甲段落内容');
    clearHighlights(root);
    highlightBlocks(root, '乙段落内容', '乙段落内容');
    expect(marked(root)).toEqual(['乙段落内容']);
  });
});
