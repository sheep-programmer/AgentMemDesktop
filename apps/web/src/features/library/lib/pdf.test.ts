import { describe, expect, it } from 'vitest';

import {
  clampPage,
  computeRenderWindow,
  isMissingFileError,
  isPdfDocument,
  matchChunkInTextItems,
  normalizeForMatch,
  resolveChunkPage,
} from './pdf';

describe('isPdfDocument', () => {
  it('mime 是 PDF 就算', () => {
    expect(isPdfDocument({ mime: 'application/pdf', title: '无扩展名' })).toBe(true);
    expect(isPdfDocument({ mime: 'Application/PDF; charset=binary' })).toBe(true);
  });

  it('没有 mime 或只有 octet-stream 的老数据退回看扩展名', () => {
    expect(isPdfDocument({ mime: null, source_uri: '/data/raw/论文.PDF' })).toBe(true);
    expect(isPdfDocument({ mime: 'application/octet-stream', meta: { raw_path: 'raw/a.pdf' } })).toBe(
      true,
    );
    expect(isPdfDocument({ title: 'paper.pdf' })).toBe(true);
  });

  it('明确是别的类型时不看扩展名：网页、粘贴文本标题带 .pdf 也不算', () => {
    expect(isPdfDocument({ mime: 'text/html', title: '下载页 report.pdf' })).toBe(false);
    expect(isPdfDocument({ mime: 'text/markdown', source_uri: 'notes.md' })).toBe(false);
    expect(isPdfDocument({ title: 'pdf 使用说明.md' })).toBe(false);
    expect(isPdfDocument(null)).toBe(false);
  });
});

describe('resolveChunkPage', () => {
  const chunks = [
    { id: 's', kind: 'summary', page: null },
    { id: 'a', kind: 'body', page: 3 },
    { id: 'b', kind: 'body', page: null },
    { id: 'c', kind: 'body', page: 5 },
  ];

  it('切片自带页码时原样返回', () => {
    expect(resolveChunkPage(chunks, 'a')).toEqual({ page: 3, exact: true });
    expect(resolveChunkPage(chunks, 'c')).toEqual({ page: 5, exact: true });
  });

  it('没有页码时借前一个正文切片的，并标成不精确', () => {
    expect(resolveChunkPage(chunks, 'b')).toEqual({ page: 3, exact: false });
  });

  it('概要切片、找不到的切片、前面也没有页码时返回 null', () => {
    expect(resolveChunkPage(chunks, 's')).toBeNull();
    expect(resolveChunkPage(chunks, 'missing')).toBeNull();
    expect(resolveChunkPage([{ id: 'x', page: null }], 'x')).toBeNull();
    expect(resolveChunkPage(chunks, null)).toBeNull();
  });

  it('非法页码（0、负数）当作没有', () => {
    expect(resolveChunkPage([{ id: 'x', page: 0 }], 'x')).toBeNull();
  });
});

describe('clampPage', () => {
  it('夹在 1 与总页数之间', () => {
    expect(clampPage(0, 10)).toBe(1);
    expect(clampPage(12, 10)).toBe(10);
    expect(clampPage(4, 10)).toBe(4);
    expect(clampPage(3, 0)).toBe(1);
  });
});

describe('computeRenderWindow', () => {
  it('可见页前后各扩一页，并包含目标页附近', () => {
    const pages = computeRenderWindow([10, 11], 300, 1, 200);
    expect([...pages].sort((a, b) => a - b)).toEqual([9, 10, 11, 12, 199, 200, 201]);
  });

  it('不越过首尾页', () => {
    expect([...computeRenderWindow([1], 2, 2)].sort()).toEqual([1, 2]);
    expect(computeRenderWindow([1], 0).size).toBe(0);
  });

  it('几百页的文档也只渲染一小段', () => {
    expect(computeRenderWindow([150], 500).size).toBe(3);
  });
});

describe('normalizeForMatch', () => {
  it('去空白标点、统一大小写与连字', () => {
    expect(normalizeForMatch('The ﬁrst  Result, 3.5%')).toBe('thefirstresult35');
    expect(normalizeForMatch('抑制剂（EGFR）的 IC50')).toBe('抑制剂egfr的ic50');
  });
});

describe('matchChunkInTextItems', () => {
  // 双栏论文的一页：左栏、右栏、页脚，文字层按条目给出，带换行连字符与多余空格
  const page = [
    'Abstract',
    'We study the selec-',
    'tivity of third generation',
    'EGFR inhibitors against',
    'T790M mutants.',
    'Unrelated right column text about',
    'pharmacokinetics in mice.',
    'Page 3',
  ];

  it('按切片正文找到对应的条目，跳过无关条目', () => {
    const match = matchChunkInTextItems(
      page,
      '**We study the selectivity** of third generation EGFR inhibitors against T790M mutants.',
    );
    expect(match?.indices).toEqual([1, 2, 3, 4]);
    expect(match?.coverage).toBeGreaterThan(0.9);
  });

  it('切片跨页时只在这一页标出属于它的那部分', () => {
    const match = matchChunkInTextItems(
      page,
      'text about pharmacokinetics in mice. 下一页才出现的中文内容，本页上不存在。',
    );
    expect(match?.indices).toEqual([5, 6]);
  });

  it('跨页切片在本页只占一小截时，连续对上几个窗口也算命中', () => {
    const match = matchChunkInTextItems(
      page,
      `We study the selectivity of third generation EGFR inhibitors. ${'后续页面的内容。'.repeat(40)}`,
    );
    expect(match?.indices).toEqual([1, 2, 3]);
    expect(match?.coverage).toBeLessThan(0.25);
  });

  it('本页没有这段文字时返回 null，不硬凑', () => {
    expect(
      matchChunkInTextItems(page, '完全无关的一段话，讲的是另一篇文献里的实验设计与统计方法。'),
    ).toBeNull();
    expect(matchChunkInTextItems([], 'anything')).toBeNull();
    expect(matchChunkInTextItems(page, '')).toBeNull();
  });

  it('套话反复出现时，只标真正那一段，不从前面同样的开头标起', () => {
    // 实测双栏样例：每段都是「研究表明……突变体 N 上……」，只有编号不同。
    // 贪心按顺序找会从第 75 段的同样开头起标，把 75–78 段全染上
    const para = (n: number) => [
      `研究表明第三代小分子抑制剂在突变体 ${n} 上`,
      '的选择性与其共价结合的丙烯酰胺弹头密切相关，药代',
      '动力学参数显示口服生物利用度随亲脂性升高而下降，',
      `编号 P${n}-${n * 7}。`,
    ];
    const items = [75, 76, 77, 78, 79].flatMap(para);
    const match = matchChunkInTextItems(
      items,
      '研究表明第三代小分子抑制剂在突变体 78 上的选择性与其共价结合的丙烯酰胺弹头密切相关，药代动力学参数显示口服生物利用度随亲脂性升高而下降，编号 P78-546。',
    );
    expect(match?.indices).toEqual([12, 13, 14, 15]);
  });

  it('续页只认页首开始、一直到切片末尾的那一截', () => {
    const chunk = `上一页的开头部分，内容很长很长。${'页首延续的后半截文字，写到切片结束为止。'}`;
    const nextPage = ['12', '页首延续的后半截文字，', '写到切片结束为止。', '下一个切片的正文从这里开始。'];
    expect(matchChunkInTextItems(nextPage, chunk, { continuation: true })?.indices).toEqual([1, 2]);

    // 同样的文字出现在页面中部：不是续页，不标
    const midPage = [
      '本页另起的一大段正文'.repeat(80),
      '页首延续的后半截文字，',
      '写到切片结束为止。',
    ];
    expect(matchChunkInTextItems(midPage, chunk, { continuation: true })).toBeNull();
    // 不加续页约束时则能在页中找到
    expect(matchChunkInTextItems(midPage, chunk)).not.toBeNull();
  });

  it('中文文字层按字切条也能对上', () => {
    const cjk = ['第', '三代', 'EGFR-TKI', '抑制剂的构效', '关系研究', '参考文献'];
    const match = matchChunkInTextItems(cjk, '## 第三代 EGFR-TKI 抑制剂的构效关系研究');
    expect(match?.indices).toEqual([0, 1, 2, 3, 4]);
  });
});

describe('isMissingFileError', () => {
  it('认出 404 与 missing 标记', () => {
    expect(isMissingFileError({ name: 'ResponseException', status: 404, missing: true })).toBe(true);
    expect(isMissingFileError({ name: 'MissingPDFException' })).toBe(true);
    expect(isMissingFileError({ name: 'ResponseException', status: 500, missing: false })).toBe(false);
    expect(isMissingFileError(new Error('boom'))).toBe(false);
    expect(isMissingFileError(null)).toBe(false);
  });
});
