import React, { useEffect, useState, useRef, useCallback, useMemo } from 'react';
import type { DocumentItem, ChunkItem } from '@/lib/api/types.temp';
import { documentService } from '@/lib/api';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { MarkdownView } from '@/components/shared/MarkdownView';
import { FileText, Loader2, Layers, Hash, Copy, Check, Sparkles, BookOpen } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';
import { markdownToPlainText } from '@/lib/markdown';
import {
  HL_ATTR,
  clearHighlights,
  clearQuoteHighlight,
  highlightBlocks,
  highlightQuote,
} from '../lib/highlight';
import { isPdfDocument, resolveChunkPage } from '../lib/pdf';
import { PdfOriginalView, type PdfJumpTarget } from './PdfOriginalView';
import { toast } from 'sonner';

export interface DocumentReaderModalProps {
  document: (Partial<DocumentItem> & { id: string; title: string; space_id?: string }) | null;
  isOpen: boolean;
  onClose: () => void;
  initialChunkId?: string;
  /** 引用依据的那一句在全文里的区间：从回答的引用点进来时有，用来精确到句高亮。 */
  initialQuote?: { start: number; end: number } | null;
}


/** 文档状态的中文说法：界面上不该出现 ready / active 这类内部取值。 */
const STATUS_LABELS: Record<string, string> = {
  pending: '排队中',
  parsing: '解析中',
  chunking: '切分中',
  embedding: '向量化中',
  extracting: '抽取中',
  ready: '已就绪',
  failed: '处理失败',
  active: '已就绪',
};

/** 上次看 PDF 时停在哪个视图：研究者多半一直看原版，不必每次打开都再点一下。 */
const VIEW_KEY = 'agentmem.readerPdfView';

type ReaderView = 'parsed' | 'pdf';

function readViewPreference(): ReaderView {
  try {
    return localStorage.getItem(VIEW_KEY) === '1' ? 'pdf' : 'parsed';
  } catch {
    return 'parsed';
  }
}

function writeViewPreference(view: ReaderView) {
  try {
    localStorage.setItem(VIEW_KEY, view === 'pdf' ? '1' : '0');
  } catch {
    // 写不进去只影响下次打开的默认视图
  }
}

/** 文档来源的中文说法。 */
const SOURCE_TYPE_LABELS: Record<string, string> = {
  file: '本地文件',
  paste: '粘贴文本',
  url: '网页抓取',
};


export function DocumentReaderModal({
  document: doc,
  isOpen,
  onClose,
  initialChunkId,
  initialQuote,
}: DocumentReaderModalProps) {
  const [content, setContent] = useState<string>('');
  const [chunks, setChunks] = useState<ChunkItem[]>([]);
  const [contextSummary, setContextSummary] = useState<string | null>(null);
  const [selectedChunkId, setSelectedChunkId] = useState<string | null>(initialChunkId || null);
  const [copiedChunkId, setCopiedChunkId] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [chunkProgress, setChunkProgress] = useState<{ loaded: number; total: number } | null>(null);
  /** 详情接口拿到的完整文档：从引用点进来时 props 里只有 id 和标题，判断 PDF 要靠它 */
  const [fullDoc, setFullDoc] = useState<DocumentItem | null>(null);
  const [view, setView] = useState<ReaderView>(readViewPreference);
  /** 原版视图第一次被切到之后就一直挂着：来回切换不必重新下载、重新渲染 */
  const [pdfMounted, setPdfMounted] = useState(false);
  const [pdfTarget, setPdfTarget] = useState<PdfJumpTarget | null>(null);
  const [pdfUnlocatedHint, setPdfUnlocatedHint] = useState<string | null>(null);
  const pdfTokenRef = useRef(0);

  const leftScrollContainerRef = useRef<HTMLDivElement>(null);
  const activeChunkRef = useRef<HTMLDivElement>(null);
  // 高亮块的位置：点右侧切片列表后要滚到正文里高亮的那一段，而不是文档开头
  const highlightAnchorRef = useRef<HTMLElement | null>(null);
  /** 待滚动的目标切片 id：高亮画好之后才兑现，且只对这一个切片生效。 */
  const pendingScrollChunkRef = useRef<string | null>(null);
  /** 当前正文里画着的高亮属于哪个切片：用来判断挂起的滚动能不能立刻兑现。 */
  const highlightedChunkRef = useRef<string | null>(null);
  const markdownContainerRef = useRef<HTMLDivElement>(null);
  /** 当前滚动是否由代码发起；反向同步在它为真时让位（滚动停下后自动复位）。 */
  const programmaticScrollRef = useRef(false);
  const programmaticScrollTimerRef = useRef<NodeJS.Timeout | null>(null);
  const scrollRafRef = useRef<number | null>(null);
  /** 首次定位做过没有，按（文档 + 进来时指定的切片）记；关掉阅读器时复位。 */
  const didInitialScrollRef = useRef<string | null>(null);

  const isSummaryChunk = useCallback((c?: ChunkItem | null): boolean => {
    if (!c) return false;
    return c.kind === 'summary' || (c.char_start === 0 && c.char_end === 0);
  }, []);

  useEffect(() => {
    if (initialChunkId) {
      setSelectedChunkId(initialChunkId);
    }
  }, [initialChunkId]);

  useEffect(() => {
    if (!doc || !isOpen) {
      setContent('');
      setChunks([]);
      setContextSummary(null);
      setSelectedChunkId(null);
      setChunkProgress(null);
      setFullDoc(null);
      setPdfTarget(null);
      setPdfUnlocatedHint(null);
      setPdfMounted(false);
      // 下次打开（哪怕是同一篇）要重新做一次首次定位
      didInitialScrollRef.current = null;
      return;
    }

    let isMounted = true;
    setIsLoading(true);
    setChunkProgress(null);

    Promise.allSettled([
      documentService.getDocumentContent(doc.id, doc.space_id),
      documentService.getAllDocumentChunks(doc.id, doc.space_id, (loaded, total) => {
        if (isMounted) {
          setChunkProgress({ loaded, total });
        }
      }),
      documentService.getDocument(doc.id, doc.space_id),
    ])
      .then(([contentRes, chunksRes, docRes]) => {
        if (!isMounted) return;

        // 正文 Markdown
        if (contentRes.status === 'fulfilled') {
          setContent(contentRes.value.markdown || '*(文档解析内容为空)*');
        } else {
          setContent('*(获取文档内容失败)*');
        }

        // 切片列表
        if (chunksRes.status === 'fulfilled') {
          const fetched = chunksRes.value || [];
          setChunks(fetched);
          if (initialChunkId && fetched.some((c) => c.id === initialChunkId)) {
            setSelectedChunkId(initialChunkId);
          } else if (!selectedChunkId && fetched.length > 0) {
            setSelectedChunkId(fetched[0].id);
          }
        } else {
          setChunks([]);
        }

        // 文档级上下文 meta.context_summary
        if (docRes.status === 'fulfilled' && docRes.value) {
          setFullDoc(docRes.value);
          const meta = docRes.value.meta as Record<string, unknown> | undefined;
          if (typeof meta?.context_summary === 'string' && meta.context_summary.trim().length > 0) {
            setContextSummary(meta.context_summary.trim());
          } else {
            setContextSummary(null);
          }
        }
      })
      .finally(() => {
        if (isMounted) setIsLoading(false);
      });

    return () => {
      isMounted = false;
    };
  }, [doc, isOpen, initialChunkId]);

  // Find currently selected chunk
  const selectedChunk = useMemo(() => {
    if (!selectedChunkId) return null;
    return chunks.find((c) => c.id === selectedChunkId) || null;
  }, [chunks, selectedChunkId]);

  const selectedChunkIndex = useMemo(() => {
    if (!selectedChunkId) return -1;
    return chunks.findIndex((c) => c.id === selectedChunkId);
  }, [chunks, selectedChunkId]);

  const isSelectedChunkSummary = useMemo(() => {
    return isSummaryChunk(selectedChunk);
  }, [selectedChunk, isSummaryChunk]);

  // 非 PDF 文档不显示切换，始终看解析文本
  const isPdf = useMemo(() => isPdfDocument({ ...doc, ...fullDoc }), [doc, fullDoc]);
  const activeView: ReaderView = isPdf ? view : 'parsed';
  const rawUrl = useMemo(
    () => (doc && isPdf ? documentService.getDocumentRawUrl(doc.id, doc.space_id) : null),
    [doc, isPdf],
  );

  useEffect(() => {
    if (activeView === 'pdf') setPdfMounted(true);
  }, [activeView]);

  /**
   * 让原版视图跳到某个切片所在页。每次都换一个 token：再点同一个切片也要能跳回去。
   * 切片自己和前面都没有页码时不跳，只在工具栏说明原因，不拿字符比例去猜页码。
   */
  const jumpPdfTo = useCallback(
    (chunk: ChunkItem | null) => {
      if (!chunk) return;
      pdfTokenRef.current += 1;
      const token = pdfTokenRef.current;
      if (isSummaryChunk(chunk)) {
        setPdfUnlocatedHint(null);
        setPdfTarget({ token, page: 1, exact: true, content: null, label: '文档概要' });
        return;
      }
      const index = chunks.findIndex((item) => item.id === chunk.id);
      const located = resolveChunkPage(chunks, chunk.id);
      if (!located) {
        setPdfTarget(null);
        setPdfUnlocatedHint(`第 ${index + 1} 段没有页码信息，无法在原版里定位`);
        return;
      }
      setPdfUnlocatedHint(null);
      setPdfTarget({
        token,
        page: located.page,
        exact: located.exact,
        content: chunk.content || null,
        label: `第 ${index + 1} 段`,
      });
    },
    [chunks, isSummaryChunk],
  );

  const switchView = useCallback(
    (next: ReaderView) => {
      setView(next);
      writeViewPreference(next);
      if (next === 'pdf') jumpPdfTo(selectedChunk);
    },
    [jumpPdfTo, selectedChunk],
  );

  /** 把左侧正文滚到高亮块的位置（留一点上边距，别贴着顶）。 */
  const scrollToHighlight = useCallback(() => {
    const container = leftScrollContainerRef.current;
    const anchor = highlightAnchorRef.current;
    if (!container || !anchor) return;
    const offset =
      anchor.getBoundingClientRect().top -
      container.getBoundingClientRect().top +
      container.scrollTop;
    container.scrollTo({ top: Math.max(0, offset - 72), behavior: 'smooth' });
  }, []);

  /**
   * 请求把正文滚到某个切片的高亮处。
   *
   * 高亮是在 Markdown 渲染完成之后才画得出来的，所以这里分两种情况：高亮已经画在
   * 这个切片上就立刻滚；还没画就登记下来，由高亮逻辑画完之后兑现。
   *
   * 必须两种都处理：只登记不立刻兑现的话，「高亮先画好、滚动请求后到」这一序
   * （文档小、渲染快时就是这个顺序）永远等不到下一次重画——重画逻辑看到已有高亮
   * 就直接返回了——于是证据点进来只高亮不跳转，正好是最常见的那条路径。
   */
  const requestScrollToChunk = useCallback(
    (chunkId: string) => {
      if (highlightedChunkRef.current === chunkId && highlightAnchorRef.current) {
        pendingScrollChunkRef.current = null;
        scrollToHighlight();
        return;
      }
      pendingScrollChunkRef.current = chunkId;
    },
    [scrollToHighlight],
  );

  // 打开文档后首次选中切片时，把正文滚到高亮处、右侧卡片对齐到视野里。
  //
  // 只在**首次**做：这个 effect 的依赖里有 selectedChunkId，而左侧正文滚动同步过来的
  // 选中项同样会触发它——那样用户刚滚到文档中段就会被拽回高亮位置（实测回拉 1730px）。
  // 「首次」按（文档 + 进来时指定的切片）算：阅读器在文库页是常驻挂载的，用一个永不
  // 复位的标志会让第二次打开起再也不定位。
  const initialScrollKey = `${doc?.id ?? ''}::${initialChunkId ?? ''}`;
  useEffect(() => {
    // 关闭的那一次渲染里切片还是上一篇的（清空要到下一次渲染才生效），而键已经变成了
    // 「无文档」——不拦住的话，会把上一篇的切片登记成下一篇原版视图的跳转目标
    if (!doc || !isOpen) return;
    if (isLoading || !selectedChunkId || chunks.length === 0) return;
    if (didInitialScrollRef.current === initialScrollKey) return;
    didInitialScrollRef.current = initialScrollKey;

    // 原版视图（哪怕此刻没显示）也先记下要去的页，切过去就在那一页
    jumpPdfTo(chunks.find((chunk) => chunk.id === selectedChunkId) ?? null);

    const timer = setTimeout(() => {
      // 概要切片没有原文区间，只需要右侧卡片对齐
      if (!isSelectedChunkSummary) {
        programmaticScrollRef.current = true;
        requestScrollToChunk(selectedChunkId);
      }
      document
        .getElementById(`chunk-card-${selectedChunkId}`)
        ?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }, 150);
    return () => clearTimeout(timer);
  }, [
    doc,
    isOpen,
    isLoading,
    selectedChunkId,
    chunks,
    isSelectedChunkSummary,
    initialScrollKey,
    requestScrollToChunk,
    jumpPdfTo,
  ]);

  const handleCopyChunk = (chunk: ChunkItem) => {
    if (chunk.content) {
      navigator.clipboard.writeText(chunk.content);
      setCopiedChunkId(chunk.id);
      toast.success('切片内容已复制');
      setTimeout(() => setCopiedChunkId(null), 1800);
    }
  };

  // 高亮区间：只算偏移，不再把正文切成三段分别渲染——切片边界一旦落在表格、
  // 列表或代码围栏内部，切开的那几段会被各自当成独立文档解析，渲染出原始 Markdown。
  const highlightRange = useMemo(() => {
    if (
      !selectedChunk ||
      isSelectedChunkSummary ||
      typeof selectedChunk.char_start !== 'number' ||
      typeof selectedChunk.char_end !== 'number' ||
      selectedChunk.char_start >= selectedChunk.char_end ||
      content.length === 0
    ) {
      return null;
    }
    const start = Math.max(0, Math.min(selectedChunk.char_start, content.length));
    const end = Math.max(start, Math.min(selectedChunk.char_end, content.length));
    return start < end ? { start, end } : null;
  }, [selectedChunk, isSelectedChunkSummary, content]);

  // 引用依据的原句：只在仍停留在「点进来的那条切片」上、且原句确实落在切片区间里时生效。
  // 切片常跨几个小节，整条高亮只能说明「在这一大段里」，原句才是答案真正的出处。
  const quote = useMemo(() => {
    if (!initialQuote || !highlightRange || selectedChunkId !== initialChunkId) return null;
    const { start, end } = initialQuote;
    if (start < highlightRange.start || end > highlightRange.end || start >= end) return null;
    return content.slice(start, end);
  }, [initialQuote, highlightRange, selectedChunkId, initialChunkId, content]);

  // 首尾文字片段：渲染后用它定位高亮范围（切片正文带着表格表头前缀，取尾部更可靠）。
  // 有原句时块级高亮只框原句所在的段落，不再铺满整条切片
  const probes = useMemo(() => {
    if (!highlightRange) return null;
    const slice = quote ?? content.slice(highlightRange.start, highlightRange.end);
    return {
      head: slice.slice(0, 48),
      tail: slice.slice(-48),
      quote,
    };
  }, [content, highlightRange, quote]);

  // 渲染完成后打高亮。Markdown 渲染器是分段提交 DOM 的，一次 effect 可能早于内容就绪，
  // 所以观察容器直到目标块出现。
  //
  // 依赖里必须带上 isLoading：正文与切片是在一次 `.then()` 里设好的，而 `isLoading`
  // 在随后的 `.finally()` 里才翻假——前一次渲染还停在加载态，左栏那个容器根本不存在，
  // effect 拿到 null 就直接返回了；不重跑的话，这一篇从头到尾都不会有高亮（后端慢一点
  // 时两次 setState 分在不同微任务里，反而看不出来，正是这种偶发最难查）。
  useEffect(() => {
    const container = markdownContainerRef.current;
    if (!container || !probes) return;

    let retries = 0;

    // 只兑现「当前这个切片」的滚动请求：换了目标就作废，避免它在下一次选中时
    // 才突然生效，把用户滚到的位置拽回去
    const consumePendingScroll = () => {
      if (pendingScrollChunkRef.current !== selectedChunk?.id) return;
      if (!highlightAnchorRef.current) return;
      pendingScrollChunkRef.current = null;
      scrollToHighlight();
    };

    const apply = () => {
      const anchor = highlightBlocks(container, probes.head, probes.tail);
      if (!anchor) return false;
      if (probes.quote) highlightQuote(container, probes.quote);
      highlightAnchorRef.current = anchor;
      highlightedChunkRef.current = selectedChunk?.id ?? null;
      consumePendingScroll();
      return true;
    };

    // 已经标好就不动它：每次 DOM 变动都「先清后画」会出现一段没有高亮的窗口，
    // 看起来就像高亮闪没了。挂起的滚动请求仍然要兑现——它可能是在高亮画好之后
    // 才登记进来的。
    const run = () => {
      if (container.querySelector(`[${HL_ATTR}]`)) {
        consumePendingScroll();
        return;
      }
      if (apply()) return;
      // 目标块还没渲染出来（或正好在被替换的中间态），稍后重试几次
      if (retries < 6) {
        retries += 1;
        window.setTimeout(run, 200);
      }
    };

    // 清掉旧高亮的同时把锚点作废：留着它会让下一次滚动跳到上一个切片的位置
    clearHighlights(container);
    highlightAnchorRef.current = null;
    highlightedChunkRef.current = null;
    run();

    const observer = new MutationObserver(run);
    observer.observe(container, { childList: true, subtree: true });
    return () => {
      observer.disconnect();
      clearQuoteHighlight();
    };
  }, [probes, selectedChunk?.id, isLoading, scrollToHighlight]);

  // Click on chunk in right column
  const handleSelectChunk = useCallback(
    (chunk: ChunkItem) => {
      setSelectedChunkId(chunk.id);
      jumpPdfTo(chunk);

      if (isSummaryChunk(chunk)) {
        // 概要切片没有原文区间，定位到文档开头即可
        programmaticScrollRef.current = true;
        leftScrollContainerRef.current?.scrollTo({ top: 0, behavior: 'smooth' });
        return;
      }

      // 普通切片：高亮已经画好就立刻滚（再点一次当前切片也要能回到它），
      // 否则登记下来等高亮画好再兑现
      programmaticScrollRef.current = true;
      requestScrollToChunk(chunk.id);
    },
    [isSummaryChunk, requestScrollToChunk, jumpPdfTo],
  );

  /**
   * 左侧正文滚动时，把右侧列表的选中项同步成「当前读到的那一段」。
   *
   * 判据是视口顶部往下一点（约三分之一处）落在哪个切片的偏移区间里——用顶部那条线
   * 更符合「我现在读到哪了」的直觉，用中心线会在长切片里来回跳。
   */
  const handleLeftScroll = useCallback(() => {
    if (chunks.length === 0) return;

    // 程序化滚动（点右侧列表、打开文档时的定位）期间不做反向同步：平滑滚动可能持续
    // 一秒以上，用固定时长保护会在长距离滚动时提前失效，把用户刚点的切片覆盖掉。
    if (programmaticScrollRef.current) {
      if (programmaticScrollTimerRef.current) {
        clearTimeout(programmaticScrollTimerRef.current);
      }
      // 滚动事件停 160ms 之后才交还控制权：平滑滚动期间事件是连续的，固定时长
      // 保护在长距离滚动时会提前失效
      programmaticScrollTimerRef.current = setTimeout(() => {
        programmaticScrollRef.current = false;
      }, 160);
      return;
    }

    if (scrollRafRef.current) {
      cancelAnimationFrame(scrollRafRef.current);
    }

    scrollRafRef.current = requestAnimationFrame(() => {
      const container = leftScrollContainerRef.current;
      if (!container) return;
      const probe = container.scrollTop + container.clientHeight * 0.33;

      const bodies = chunks.filter(
        (chunk) =>
          typeof chunk.char_start === 'number' &&
          typeof chunk.char_end === 'number' &&
          chunk.char_end > chunk.char_start,
      );
      if (bodies.length === 0) return;
      const current =
        bodies.find((chunk) => probe >= (chunk.char_start ?? 0) && probe < (chunk.char_end ?? 0)) ??
        bodies[bodies.length - 1];
      if (current.id === selectedChunkId) return;

      setSelectedChunkId(current.id);
      document
        .getElementById(`chunk-card-${current.id}`)
        ?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    });
  }, [chunks, selectedChunkId]);

  return (
    <Dialog open={isOpen} onOpenChange={(open) => !open && onClose()}>
      {/* 阅读器默认占满整屏：看长文档、对切片本来就需要地方；关掉走右上角的叉 */}
      <DialogContent className="!max-w-none w-screen h-screen sm:!max-w-none flex flex-col p-0 gap-0 rounded-none border-0 overflow-hidden bg-card">
        <DialogHeader className="px-6 py-3.5 border-b border-border/80 flex flex-row items-center justify-between shrink-0 bg-muted/10">
          <div className="flex items-center gap-3">
            <div className="flex h-8 w-8 items-center justify-center rounded-md bg-primary/10 text-primary">
              <FileText className="h-4 w-4" />
            </div>
            <div>
              <DialogTitle className="text-base font-semibold leading-none truncate max-w-[600px]">
                {doc?.title || '文档阅读与切片对齐'}
              </DialogTitle>
              <div className="flex items-center gap-2 mt-1 text-xs text-muted-foreground">
                <span>{SOURCE_TYPE_LABELS[doc?.source_type ?? ''] ?? '文件'}</span>
                <span>·</span>
                <span>状态：{STATUS_LABELS[doc?.status ?? ''] ?? '已就绪'}</span>
                {doc?.token_count ? (
                  <>
                    <span>·</span>
                    <span>{doc.token_count.toLocaleString()} 词元</span>
                  </>
                ) : null}
                <span>·</span>
                <span className="rounded bg-primary/10 px-1.5 py-0.2 font-mono text-[10px] text-primary">
                  {isLoading && chunkProgress
                    ? `正在同步切片：${chunkProgress.loaded}/${chunkProgress.total}`
                    : `${chunks.length} 个切片`}
                </span>
                {chunks.length > 500 && (
                  <span className="flex items-center gap-1 rounded bg-amber-500/10 px-1.5 py-0.2 font-mono text-[10px] text-amber-700 dark:text-amber-400">
                    <Sparkles className="h-3 w-3" />
                    超大文档全量对齐 ({chunks.length})
                  </span>
                )}
              </div>
            </div>
          </div>
        </DialogHeader>

        {/* 文档级上下文（系统把这篇文档理解成了什么，为空时不显示） */}
        {contextSummary && (
          <div className="border-b border-border/70 bg-primary/5 px-6 py-2.5 text-xs select-none">
            <div className="flex items-start gap-2.5">
              <Sparkles className="h-4 w-4 text-primary shrink-0 mt-0.5" />
              <div className="space-y-0.5 flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="font-semibold text-foreground text-xs">文档概要</span>
                  <Badge variant="outline" className="text-[10px] px-1.5 py-0 bg-primary/15 text-primary border-primary/30">
                    导入时自动生成
                  </Badge>
                </div>
                <p className="text-muted-foreground text-[11px] leading-relaxed">
                  {contextSummary}
                </p>
              </div>
            </div>
          </div>
        )}

        {/* 双栏布局 */}
        <div className="flex flex-1 overflow-hidden">
          {/* 左栏：解析后的 Markdown 正文；PDF 文档可切到原版排版 */}
          <div className="flex-1 flex flex-col border-r border-border min-w-0 overflow-hidden">
            <div className="flex items-center justify-between border-b border-border/50 bg-muted/20 px-4 py-2 text-xs text-muted-foreground font-medium">
              {isPdf ? (
                <div
                  role="tablist"
                  aria-label="阅读视图"
                  className="flex items-center rounded-md border border-border/70 bg-background/60 p-0.5"
                >
                  {(
                    [
                      ['parsed', '解析文本'],
                      ['pdf', '原版 PDF'],
                    ] as const
                  ).map(([value, label]) => (
                    <button
                      key={value}
                      type="button"
                      role="tab"
                      aria-selected={activeView === value}
                      onClick={() => switchView(value)}
                      className={cn(
                        'rounded px-2.5 py-0.5 text-[11px] transition-colors',
                        activeView === value
                          ? 'bg-primary/15 text-primary font-semibold'
                          : 'text-muted-foreground hover:text-foreground',
                      )}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              ) : (
                <span>全文</span>
              )}
              <span className="text-[11px]">
                {activeView === 'pdf'
                  ? pdfTarget
                    ? pdfTarget.content == null
                      ? '当前查看：文档概要'
                      : `当前定位：${pdfTarget.label} · 第 ${pdfTarget.page} 页`
                    : '原始排版与图表，适合核对双栏论文'
                  : selectedChunk
                    ? isSelectedChunkSummary
                      ? '当前查看：文档概要'
                      : `当前高亮：第 ${selectedChunkIndex + 1} 段`
                    : '支持公式、多级标题渲染与双向视口联动'}
              </span>
            </div>
            {pdfMounted && (
              <div className={cn('flex-1 min-h-0', activeView !== 'pdf' && 'hidden')}>
                <PdfOriginalView
                  url={rawUrl}
                  target={pdfTarget}
                  unlocatedHint={pdfUnlocatedHint}
                  onUseParsedView={() => switchView('parsed')}
                />
              </div>
            )}
            {/* 解析文本在原版视图下只是藏起来：高亮与滚动位置都留着，切回来不用重算 */}
            <div
              ref={leftScrollContainerRef}
              onScroll={handleLeftScroll}
              className={cn('flex-1 p-6 overflow-y-auto scroll-smooth', activeView !== 'parsed' && 'hidden')}
            >
              {isLoading ? (
                <div className="flex flex-col h-full items-center justify-center gap-2 text-sm text-muted-foreground">
                  <Loader2 className="h-5 w-5 animate-spin text-primary" />
                  <span>正在加载解析全文与全量切片…</span>
                  {chunkProgress && (
                    <span className="text-xs text-muted-foreground font-mono">
                      已获取切片：{chunkProgress.loaded} / 共 {chunkProgress.total} 条
                    </span>
                  )}
                </div>
              ) : highlightRange ? (
                <div className="space-y-2">
                  {/* 精确按 char_start / char_end 高亮渲染的目标切片 */}
                  <div
                    ref={activeChunkRef}
                    id={`chunk-highlight-${selectedChunk?.id}`}
                    data-chunk-id={selectedChunk?.id}
                    className="mb-3 rounded-xl border-2 border-primary/50 bg-primary/8 p-3.5 shadow-sm ring-1 ring-primary/20 transition-all text-foreground"
                  >
                    <div className="flex items-center justify-between text-xs">
                      <span className="flex items-center gap-1.5 font-mono font-semibold text-primary">
                        <Hash className="h-3.5 w-3.5" />
                        第 {selectedChunkIndex + 1} 段
                      </span>
                      <span className="font-mono text-[10px] text-primary bg-muted rounded px-2 py-0.5">
                        {selectedChunk?.token_count ||
                          Math.ceil((highlightRange.end - highlightRange.start) / 3)}{' '}
                        词元
                      </span>
                    </div>
                    {quote && (
                      <p className="mt-2 border-l-2 border-primary/60 pl-2.5 text-[12.5px] leading-relaxed text-foreground/90">
                        <span className="mr-1.5 text-[11px] font-medium text-primary">依据原句</span>
                        {quote}
                      </p>
                    )}
                  </div>

                  <div
                    ref={markdownContainerRef}
                    className="prose prose-sm dark:prose-invert max-w-none"
                  >
                    <MarkdownView>{content}</MarkdownView>
                  </div>
                </div>
              ) : (
                <div className="space-y-4">
                  {isSelectedChunkSummary && (
                    <div className="rounded-xl border border-primary/30 bg-primary/10 p-3.5 text-xs text-foreground flex items-center gap-2.5">
                      <BookOpen className="h-4 w-4 text-primary shrink-0" />
                      <div>
                        <div className="font-semibold text-primary">当前选中的是文档概要</div>
                        <p className="text-[11px] text-muted-foreground mt-0.5">
                          概要是对整篇文档的总结，不对应原文里的某一段，下面展示完整原文。
                        </p>
                      </div>
                    </div>
                  )}
                  <div className="prose prose-sm dark:prose-invert max-w-none">
                    <MarkdownView>{content}</MarkdownView>
                  </div>
                </div>
              )}
            </div>
          </div>

          {/* 右栏：分块切片（Chunks）列表与溯源对齐 */}
          <div className="w-[380px] shrink-0 flex flex-col bg-card/30 min-w-0 overflow-hidden">
            <div className="flex items-center justify-between border-b border-border/50 bg-muted/30 px-4 py-2 text-xs font-medium text-foreground">
              <div className="flex items-center gap-1.5">
                <Layers className="h-3.5 w-3.5 text-primary" />
                <span>段落列表</span>
              </div>
              <span className="text-[11px] text-muted-foreground font-mono">
                {isLoading && chunkProgress ? `${chunkProgress.loaded}/${chunkProgress.total}` : `${chunks.length}`} 个切片
              </span>
            </div>

            <div className="flex-1 p-3 overflow-y-auto space-y-2.5">
              {isLoading ? (
                <div className="flex flex-col h-40 items-center justify-center gap-2 text-xs text-muted-foreground">
                  <Loader2 className="h-4 w-4 animate-spin text-primary" />
                  <span>正在加载切片列表...</span>
                  {chunkProgress && (
                    <span className="font-mono text-[11px]">
                      已拉取 {chunkProgress.loaded} / {chunkProgress.total} 条
                    </span>
                  )}
                </div>
              ) : chunks.length === 0 ? (
                <div className="py-12 text-center text-xs text-muted-foreground">
                  暂无切片数据
                </div>
              ) : (
                chunks.map((chunk, idx) => {
                  const isSelected = selectedChunkId === chunk.id;
                  const isSummary = isSummaryChunk(chunk);

                  return (
                    <div
                      key={chunk.id}
                      id={`chunk-card-${chunk.id}`}
                      onClick={() => handleSelectChunk(chunk)}
                      className={cn(
                        'group relative rounded-lg border p-3 text-xs transition-all cursor-pointer select-none',
                        isSelected
                          ? 'border-primary border-l-4 bg-primary/10 ring-1 ring-primary/40 shadow-sm'
                          : 'border-border/70 bg-card hover:border-border hover:shadow-2xs',
                      )}
                    >
                      <div className="flex items-center justify-between gap-1 mb-1.5">
                        <div className="flex items-center gap-1.5">
                          <span className="flex items-center gap-1 font-mono text-[10px] font-semibold text-primary">
                            <Hash className="h-3 w-3" />
                            切片 {idx + 1}
                          </span>
                          {isSummary && (
                            <Badge
                              variant="outline"
                              className="text-[10px] px-1.5 py-0 bg-primary/15 text-primary border-primary/30"
                            >
                              文档概要
                            </Badge>
                          )}
                        </div>

                        <div className="flex items-center gap-1.5 text-[10px] text-muted-foreground">
                          <button
                            type="button"
                            onClick={(e) => {
                              e.stopPropagation();
                              handleCopyChunk(chunk);
                            }}
                            className="opacity-0 group-hover:opacity-100 p-0.5 hover:text-foreground rounded transition-opacity"
                            title="复制切片"
                          >
                            {copiedChunkId === chunk.id ? (
                              <Check className="h-3 w-3 text-accent-insight" />
                            ) : (
                              <Copy className="h-3 w-3" />
                            )}
                          </button>
                          <span className="font-mono">
                            {chunk.token_count || Math.ceil((chunk.content || '').length / 3)} 词元
                          </span>
                        </div>
                      </div>

                      {chunk.heading_path && (
                        <div className="text-[10px] text-primary font-medium mb-1 truncate">
                          {chunk.heading_path}
                        </div>
                      )}

                      {/* 摘要用纯文本：列表是索引，露出 Markdown 记号（表格竖线、
                          井号）反而更难扫读；原文在左侧渲染视图里看 */}
                      <p
                        className={cn(
                          'text-[11px] line-clamp-3 leading-relaxed',
                          isSelected ? 'text-foreground' : 'text-muted-foreground',
                        )}
                      >
                        {markdownToPlainText(chunk.content || '')}
                      </p>
                    </div>
                  );
                })
              )}
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
