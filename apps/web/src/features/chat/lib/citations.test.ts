import { describe, expect, it } from 'vitest';
import { describeLocation, quoteRangeOf, sectionOf } from './citations';

describe('sectionOf', () => {
  it('drops a first-level heading that is just the document title', () => {
    expect(sectionOf('长江大学 > 历史沿革', '长江大学.md')).toBe('历史沿革');
  });

  it('drops a document H1 that is a longer form of the title', () => {
    expect(
      sectionOf('星澜 X3 储能系统技术手册 > 三、故障代码 > 3.1 E07 过温保护', '星澜 X3 技术手册'),
    ).toBe('三、故障代码 › 3.1 E07 过温保护');
  });

  it('keeps an unrelated first-level heading', () => {
    expect(sectionOf('第一章 总则 > 1.1 目的', '员工手册')).toBe('第一章 总则 › 1.1 目的');
  });
});

describe('quoteRangeOf', () => {
  it('returns the quote range only when both ends are valid', () => {
    expect(quoteRangeOf({ quote_start: 3, quote_end: 9 })).toEqual({ start: 3, end: 9 });
    expect(quoteRangeOf({ quote_start: 3, quote_end: null })).toBeNull();
    expect(quoteRangeOf({ quote_start: 9, quote_end: 9 })).toBeNull();
  });
});

it('describes a summary chunk without a section', () => {
  expect(describeLocation({ kind: 'summary', heading_path: 'x' })).toBe('文档概要');
});
