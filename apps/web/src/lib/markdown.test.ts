import { describe, expect, it } from 'vitest';

import { citationLabel, markdownToPlainText, withCitationMarkers } from './markdown';

describe('citationLabel', () => {
  it('只显示序号，脱掉 [^c1] 这种脚注写法', () => {
    expect(citationLabel('c1')).toBe('1');
    expect(citationLabel('^c12')).toBe('12');
  });

  it('认不出的标记原样返回，不会显示成空白', () => {
    expect(citationLabel('note')).toBe('note');
    expect(citationLabel('c')).toBe('c');
  });
});

describe('withCitationMarkers', () => {
  it('按位置把芯片插回正文', () => {
    const out = withCitationMarkers('结论一。结论二。', [
      { marker: 'c1', char_offset: 4 },
      { marker: 'c2', char_offset: 8 },
    ]);
    expect(out).toBe('结论一。[^c1]结论二。[^c2]');
  });

  it('乱序传入也按位置从前往后插', () => {
    const out = withCitationMarkers('甲乙丙', [
      { marker: 'c2', char_offset: 3 },
      { marker: 'c1', char_offset: 1 },
    ]);
    expect(out).toBe('甲[^c1]乙丙[^c2]');
  });

  it('没有位置的引用被跳过：宁可不插，也不能插错地方', () => {
    // 后端对旧数据可能只有标记没有偏移，插到 0 会让「哪句话有出处」彻底失真
    expect(withCitationMarkers('一段正文', [{ marker: 'c1' }])).toBe('一段正文');
    expect(withCitationMarkers('一段正文', [{ marker: 'c1', char_offset: null }])).toBe('一段正文');
  });

  it('越界的位置也跳过', () => {
    expect(withCitationMarkers('短', [{ marker: 'c1', char_offset: 99 }])).toBe('短');
    expect(withCitationMarkers('短', [{ marker: 'c1', char_offset: -1 }])).toBe('短');
  });

  it('空正文或空引用直接原样返回', () => {
    expect(withCitationMarkers('', [{ marker: 'c1', char_offset: 0 }])).toBe('');
    expect(withCitationMarkers('正文', [])).toBe('正文');
    expect(withCitationMarkers('正文', undefined)).toBe('正文');
  });
});

describe('markdownToPlainText', () => {
  it('列表摘要里不该出现 Markdown 记号', () => {
    const source = '## 标题\n\n- 一条**要点**\n\n| 列 | 值 |\n| --- | --- |\n| a | 1 |\n';
    const plain = markdownToPlainText(source);
    expect(plain).not.toMatch(/[#*|]/);
    expect(plain).toContain('标题');
    expect(plain).toContain('要点');
  });

  it('代码块整段丢掉，行内代码留内容', () => {
    expect(markdownToPlainText('说明 `IC50` 值\n\n```py\nprint(1)\n```')).toBe('说明 IC50 值');
  });

  it('链接留文字、图片丢掉、引用标记不残留', () => {
    expect(markdownToPlainText('见[文档](http://x)与![图](y.png)[^c1]')).toBe('见文档与');
  });

  it('空输入返回空串', () => {
    expect(markdownToPlainText('')).toBe('');
  });
});
