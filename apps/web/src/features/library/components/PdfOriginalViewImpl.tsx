import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  GlobalWorkerOptions,
  TextLayer,
  getDocument,
  type PDFDocumentProxy,
  type RenderTask,
} from 'pdfjs-dist';
// worker 由 Vite 当静态资源产出（带哈希），构建产物里能直接加载到
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url';
import { FileX, FileWarning, Loader2, MapPin, MoveHorizontal, ZoomIn, ZoomOut } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import {
  clampPage,
  computeRenderWindow,
  isMissingFileError,
  matchChunkInTextItems,
} from '../lib/pdf';
import './pdf-text-layer.css';

GlobalWorkerOptions.workerSrc = workerUrl;

/** CMap、标准字体、wasm 由 vite.config.ts 里的 pdfjsAssets 插件放到 /pdfjs/ 下。 */
const ASSET_BASE = `${import.meta.env.BASE_URL}pdfjs/`;

const ZOOM_MIN = 0.5;
const ZOOM_MAX = 3;
const ZOOM_STEP = 0.25;
/** 页与页之间的间距（px），估算占位高度与跳页偏移都要算进去。 */
const PAGE_GAP = 16;

/** 阅读器要原版视图跳去的地方。token 每次请求都变：再点一次同一个切片也要能跳回去。 */
export interface PdfJumpTarget {
  token: number;
  page: number;
  /** false：切片没有自己的页码，借用了前一段的 */
  exact: boolean;
  /** 要在文字层里找的切片正文；概要切片为 null（只回到第一页） */
  content: string | null;
  /** 页面顶部标注用的说法，如「第 3 段」 */
  label: string;
}

export interface PdfOriginalViewProps {
  /** 原始文件地址；null 表示这里拿不到原件（示例模式） */
  url: string | null;
  target: PdfJumpTarget | null;
  /** 当前选中切片没有任何页码可用时的提示（不跳页，只告诉用户为什么） */
  unlocatedHint?: string | null;
  onUseParsedView: () => void;
}

type LoadState =
  | { kind: 'loading'; percent: number | null }
  | { kind: 'ready' }
  | { kind: 'missing' }
  | { kind: 'error'; message: string };

interface PageSize {
  width: number;
  height: number;
}

/**
 * 文字层匹配的结果：页码 → 是否在这一页标出了切片文字。
 *
 * 按跳转 token 归档，而不是在 token 变化时用 effect 清空：子组件的 effect 先于父组件
 * 执行，页面刚上报的结果会被随后的清空抹掉，标注就一直停在「正在对照」。
 */
interface MatchResult {
  token: number;
  pages: Record<number, boolean>;
}

function describeLoadError(error: unknown): string {
  const name = (error as { name?: string } | null)?.name;
  if (name === 'PasswordException') return '这份 PDF 设置了打开密码，阅读器暂不支持。';
  if (name === 'InvalidPDFException') return '原始文件不是有效的 PDF，可能已损坏或被替换。';
  const message = (error as { message?: string } | null)?.message;
  return message ? `原版 PDF 加载失败：${message}` : '原版 PDF 加载失败。';
}

export function PdfOriginalViewImpl({
  url,
  target,
  unlocatedHint,
  onUseParsedView,
}: PdfOriginalViewProps) {
  const [state, setState] = useState<LoadState>(
    url ? { kind: 'loading', percent: null } : { kind: 'missing' },
  );
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  /** 第一页的尺寸（scale = 1）：没渲染过的页按它估占位高度 */
  const [baseSize, setBaseSize] = useState<PageSize | null>(null);
  /** 渲染过的页记下真实尺寸，混排横竖页时占位才不会一直偏 */
  const [pageSizes, setPageSizes] = useState<Record<number, PageSize>>({});
  const [containerWidth, setContainerWidth] = useState(0);
  const [zoom, setZoom] = useState(1);
  const [visiblePages, setVisiblePages] = useState<number[]>([1]);
  const [currentPage, setCurrentPage] = useState(1);
  const [matches, setMatches] = useState<MatchResult>({ token: -1, pages: {} });

  const scrollRef = useRef<HTMLDivElement>(null);
  const pageElements = useRef(new Map<number, HTMLDivElement>());
  /** 还没兑现的跳转：页先到位，文字层高亮画好后再细调到高亮处 */
  const pendingJumpRef = useRef<PdfJumpTarget | null>(null);

  const numPages = pdf?.numPages ?? 0;

  // ---- 加载文档 ----
  useEffect(() => {
    if (!url) {
      setState({ kind: 'missing' });
      return;
    }
    setState({ kind: 'loading', percent: null });
    setPdf(null);
    setBaseSize(null);
    setPageSizes({});
    setMatches({ token: -1, pages: {} });

    // 后端 raw 接口支持 Range：关掉整份预取和流式下载，pdf.js 只按页去取需要的字节段，
    // 几百页的扫描件也不用等全部下完才出第一页
    const task = getDocument({
      url,
      disableAutoFetch: true,
      disableStream: true,
      cMapUrl: `${ASSET_BASE}cmaps/`,
      cMapPacked: true,
      standardFontDataUrl: `${ASSET_BASE}standard_fonts/`,
      wasmUrl: `${ASSET_BASE}wasm/`,
      iccUrl: `${ASSET_BASE}iccs/`,
      enableXfa: false,
    });
    task.onProgress = ({ loaded, total }: { loaded: number; total: number }) => {
      if (total > 0) {
        setState((prev) =>
          prev.kind === 'loading' ? { kind: 'loading', percent: Math.round((loaded / total) * 100) } : prev,
        );
      }
    };

    let cancelled = false;
    task.promise
      .then(async (doc) => {
        const first = await doc.getPage(1);
        const viewport = first.getViewport({ scale: 1 });
        if (cancelled) return;
        setBaseSize({ width: viewport.width, height: viewport.height });
        setPdf(doc);
        setState({ kind: 'ready' });
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setState(isMissingFileError(error) ? { kind: 'missing' } : { kind: 'error', message: describeLoadError(error) });
      });

    return () => {
      cancelled = true;
      void task.destroy();
    };
  }, [url]);

  // ---- 适配宽度 ----
  useEffect(() => {
    const element = scrollRef.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => {
      // 视图被切走（display: none）时宽度是 0，别拿它去算缩放
      const width = entry.contentRect.width;
      if (width > 0) setContainerWidth(width);
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [state.kind]);

  /** 「适合宽度」时的缩放；论文页宽约 612pt，宽屏上不必放到撑满，封顶 1.6 倍。 */
  const fitScale = useMemo(() => {
    if (!baseSize || containerWidth <= 0) return 1;
    return Math.min(1.6, Math.max(0.3, (containerWidth - 48) / baseSize.width));
  }, [baseSize, containerWidth]);
  const scale = fitScale * zoom;

  const sizeOf = useCallback(
    (page: number): PageSize | null => pageSizes[page] ?? baseSize,
    [pageSizes, baseSize],
  );

  // ---- 可见页 ----
  useEffect(() => {
    const root = scrollRef.current;
    if (!root || numPages === 0) return;
    const visible = new Set<number>();
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          const page = Number((entry.target as HTMLElement).dataset.page);
          if (entry.isIntersecting) visible.add(page);
          else visible.delete(page);
        }
        const sorted = [...visible].sort((a, b) => a - b);
        if (sorted.length > 0) setVisiblePages(sorted);
      },
      // 提前一屏开始画，滚动时不至于看到空白页
      { root, rootMargin: '100% 0px' },
    );
    pageElements.current.forEach((element) => observer.observe(element));
    return () => observer.disconnect();
  }, [numPages]);

  /**
   * 工具栏上的「第几页」：看视口上方三分之一那条线落在哪一页。
   * 不能拿可见页里最小的那个——观察器为了提前渲染把上下各一屏都算作可见，会少报一页。
   */
  const scrollRafRef = useRef<number | null>(null);
  const handleScroll = useCallback(() => {
    if (scrollRafRef.current != null) return;
    scrollRafRef.current = requestAnimationFrame(() => {
      scrollRafRef.current = null;
      const container = scrollRef.current;
      if (!container) return;
      const probe = container.scrollTop + container.clientHeight * 0.33;
      let found = 1;
      for (const [page, element] of pageElements.current) {
        if (element.offsetTop <= probe && page > found) found = page;
      }
      setCurrentPage(found);
    });
  }, []);
  useEffect(
    () => () => {
      if (scrollRafRef.current != null) cancelAnimationFrame(scrollRafRef.current);
    },
    [],
  );

  const renderWindow = useMemo(
    () => computeRenderWindow(visiblePages, numPages, 1, target?.page ?? null),
    [visiblePages, numPages, target?.page],
  );

  // ---- 跳页 ----
  const scrollToPage = useCallback((page: number) => {
    const container = scrollRef.current;
    const element = pageElements.current.get(page);
    if (!container || !element) return;
    container.scrollTo({ top: Math.max(0, element.offsetTop - PAGE_GAP) });
  }, []);

  useEffect(() => {
    if (!target || !pdf) return;
    const page = clampPage(target.page, pdf.numPages);
    // 概要没有要找的文字，跳到页就算完成；留着挂起会被后面别的页误兑现
    pendingJumpRef.current = target.content ? { ...target, page } : null;
    scrollToPage(page);
    setCurrentPage(page);
  }, [target, pdf, scrollToPage]);

  // 缩放之后当前页位置会漂，拉回到缩放前看的那一页
  const zoomAnchorRef = useRef<number | null>(null);
  const changeZoom = (next: number) => {
    zoomAnchorRef.current = currentPage;
    setZoom(Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, next)));
  };
  useEffect(() => {
    if (zoomAnchorRef.current == null) return;
    scrollToPage(zoomAnchorRef.current);
    zoomAnchorRef.current = null;
  }, [scale, scrollToPage]);

  /** 某页文字层对完了：记下结果；它若是待兑现跳转的目标页，就细调到高亮处。 */
  const handleMatched = useCallback((page: number, anchor: HTMLElement | null, token: number) => {
    const found = Boolean(anchor);
    setMatches((prev) => {
      if (prev.token !== token) return { token, pages: { [page]: found } };
      return prev.pages[page] === found ? prev : { token, pages: { ...prev.pages, [page]: found } };
    });
    const pending = pendingJumpRef.current;
    if (!pending || pending.page !== page || pending.token !== token) return;
    pendingJumpRef.current = null;
    const container = scrollRef.current;
    if (!anchor || !container) return;
    const offset =
      anchor.getBoundingClientRect().top - container.getBoundingClientRect().top + container.scrollTop;
    container.scrollTo({ top: Math.max(0, offset - 96), behavior: 'smooth' });
  }, []);

  const handleSize = useCallback((page: number, size: PageSize) => {
    setPageSizes((prev) => {
      const known = prev[page];
      if (known && known.width === size.width && known.height === size.height) return prev;
      return { ...prev, [page]: size };
    });
  }, []);

  const registerPage = useCallback((page: number, element: HTMLDivElement | null) => {
    if (element) pageElements.current.set(page, element);
    else pageElements.current.delete(page);
  }, []);

  // ---- 渲染 ----
  if (state.kind === 'missing' || state.kind === 'error') {
    const missing = state.kind === 'missing';
    return (
      <div className="flex h-full items-center justify-center p-8">
        <div className="max-w-md rounded-xl border border-border bg-muted/20 p-6 text-center">
          <div className="mx-auto mb-3 flex h-10 w-10 items-center justify-center rounded-full bg-muted text-muted-foreground">
            {missing ? <FileX className="h-5 w-5" /> : <FileWarning className="h-5 w-5" />}
          </div>
          <div className="text-sm font-semibold text-foreground">
            {missing ? '找不到这篇文档的原始 PDF' : '原版 PDF 打不开'}
          </div>
          <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
            {missing
              ? url
                ? '早期导入的数据可能没有保留原件，或者原件已被移动、删除。解析出的全文和切片不受影响。'
                : '示例模式没有连接后端，拿不到原始文件。'
              : state.message}
          </p>
          <Button size="sm" variant="outline" className="mt-4" onClick={onUseParsedView}>
            改看解析文本
          </Button>
        </div>
      </div>
    );
  }

  const targetPage = target && numPages > 0 ? clampPage(target.page, numPages) : null;
  const matched = target && matches.token === target.token ? matches.pages : {};
  // 切片跨页时，后半截在下一页的开头，也去那一页找一找（按续页的规矩找）
  const highlightFor = (page: number): string | null => {
    if (!target?.content || targetPage == null) return null;
    return page === targetPage || page === targetPage + 1 ? target.content : null;
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center justify-between gap-2 border-b border-border/50 bg-muted/10 px-4 py-1.5 text-[11px] text-muted-foreground">
        <span className="font-mono">
          {numPages > 0 ? `第 ${currentPage} / ${numPages} 页` : '正在打开…'}
        </span>
        {unlocatedHint && <span className="truncate text-amber-700 dark:text-amber-400">{unlocatedHint}</span>}
        <div className="flex items-center gap-1">
          <Button
            size="icon-xs"
            variant="ghost"
            title="缩小"
            aria-label="缩小"
            disabled={zoom <= ZOOM_MIN}
            onClick={() => changeZoom(zoom - ZOOM_STEP)}
          >
            <ZoomOut />
          </Button>
          <span className="w-10 text-center font-mono">{Math.round(scale * 100)}%</span>
          <Button
            size="icon-xs"
            variant="ghost"
            title="放大"
            aria-label="放大"
            disabled={zoom >= ZOOM_MAX}
            onClick={() => changeZoom(zoom + ZOOM_STEP)}
          >
            <ZoomIn />
          </Button>
          <Button
            size="icon-xs"
            variant="ghost"
            title="适合宽度"
            aria-label="适合宽度"
            disabled={zoom === 1}
            onClick={() => changeZoom(1)}
          >
            <MoveHorizontal />
          </Button>
        </div>
      </div>

      <div
        ref={scrollRef}
        onScroll={handleScroll}
        className="relative min-h-0 flex-1 overflow-auto bg-muted/30 px-6 py-4"
      >
        {state.kind === 'loading' || !pdf || !baseSize ? (
          <div className="flex h-full flex-col items-center justify-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-5 w-5 animate-spin text-primary" />
            <span>正在打开原版 PDF…</span>
            {state.kind === 'loading' && state.percent != null && (
              <span className="font-mono text-xs">{state.percent}%</span>
            )}
          </div>
        ) : (
          <div className="flex flex-col items-center" style={{ gap: PAGE_GAP }}>
            {Array.from({ length: numPages }, (_, index) => {
              const page = index + 1;
              const size = sizeOf(page) ?? baseSize;
              const isTarget = page === targetPage;
              return (
                <PdfPage
                  key={page}
                  pdf={pdf}
                  page={page}
                  scale={scale}
                  size={size}
                  active={renderWindow.has(page)}
                  highlight={highlightFor(page)}
                  continuation={targetPage != null && page === targetPage + 1}
                  highlightToken={highlightFor(page) ? (target?.token ?? 0) : 0}
                  onMatched={handleMatched}
                  onSize={handleSize}
                  register={registerPage}
                  banner={
                    isTarget && target ? (
                      <TargetBanner
                        label={target.label}
                        exact={target.exact}
                        summary={target.content == null}
                        matched={
                          matched[page] || matched[page + 1] ? true : matched[page] === false ? false : undefined
                        }
                      />
                    ) : null
                  }
                />
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

/** 目标页顶部的标注：告诉用户这一页为什么被选中、文字有没有标出来。 */
function TargetBanner({
  label,
  exact,
  summary,
  matched,
}: {
  label: string;
  exact: boolean;
  summary: boolean;
  matched: boolean | undefined;
}) {
  let note: string;
  if (summary) note = '概要不对应原文里的某一段，已回到第一页';
  else if (matched) note = '已在页面上标出切片文字';
  else if (matched === false) note = '文字层没能对上切片（扫描件或公式较多），只定位到页';
  else note = '正在对照页面文字…';
  return (
    <div className="pointer-events-none absolute -top-px left-0 right-0 z-10 flex items-center gap-1.5 rounded-t-sm bg-primary px-2.5 py-1 text-[11px] text-primary-foreground shadow-sm">
      <MapPin className="h-3 w-3 shrink-0" />
      <span className="font-semibold">切片所在页</span>
      <span className="opacity-80">· {label}</span>
      {!exact && !summary && <span className="opacity-80">· 该段没有记录页码，按前一段推断</span>}
      <span className="ml-auto truncate opacity-80">{note}</span>
    </div>
  );
}

interface PdfPageProps {
  pdf: PDFDocumentProxy;
  page: number;
  scale: number;
  /** scale = 1 时的尺寸：没渲染前用它撑占位 */
  size: PageSize;
  /** 是否在渲染窗口里；窗口外只留占位框，画布释放掉 */
  active: boolean;
  highlight: string | null;
  /** 这一页是目标页的下一页：只认页首延续过来的后半截 */
  continuation: boolean;
  /** 跳转请求的 token：同一段文字再次被请求（切回原版、再点一次）时也要重新对照并上报 */
  highlightToken: number;
  banner: React.ReactNode;
  onMatched: (page: number, anchor: HTMLElement | null, token: number) => void;
  onSize: (page: number, size: PageSize) => void;
  register: (page: number, element: HTMLDivElement | null) => void;
}

/** 单页：画布 + 文字层。离开渲染窗口时清掉画布，几百页也只占十来页的内存。 */
const PdfPage = memo(function PdfPage({
  pdf,
  page,
  scale,
  size,
  active,
  highlight,
  continuation,
  highlightToken,
  banner,
  onMatched,
  onSize,
  register,
}: PdfPageProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const textRef = useRef<HTMLDivElement>(null);
  const textLayerRef = useRef<TextLayer | null>(null);
  /** 文字层画好一次加一，高亮 effect 靠它知道可以开始对照了 */
  const [textVersion, setTextVersion] = useState(0);
  const [rendering, setRendering] = useState(false);

  const setRoot = useCallback((element: HTMLDivElement | null) => register(page, element), [register, page]);

  useEffect(() => {
    const canvas = canvasRef.current;
    const textContainer = textRef.current;
    if (!canvas || !textContainer) return;

    const release = () => {
      textLayerRef.current?.cancel();
      textLayerRef.current = null;
      textContainer.replaceChildren();
      // 把位图尺寸归零，浏览器才会真正释放这块显存
      canvas.width = 0;
      canvas.height = 0;
    };

    if (!active) {
      release();
      return;
    }

    let cancelled = false;
    let renderTask: RenderTask | null = null;
    setRendering(true);

    (async () => {
      const proxy = await pdf.getPage(page);
      if (cancelled) return;
      const base = proxy.getViewport({ scale: 1 });
      onSize(page, { width: base.width, height: base.height });

      const viewport = proxy.getViewport({ scale });
      const ratio = window.devicePixelRatio || 1;
      canvas.width = Math.floor(viewport.width * ratio);
      canvas.height = Math.floor(viewport.height * ratio);
      canvas.style.width = `${Math.floor(viewport.width)}px`;
      canvas.style.height = `${Math.floor(viewport.height)}px`;

      renderTask = proxy.render({
        canvas,
        viewport,
        transform: ratio !== 1 ? [ratio, 0, 0, ratio, 0, 0] : undefined,
      });
      await renderTask.promise;
      if (cancelled) return;
      setRendering(false);

      textLayerRef.current?.cancel();
      textContainer.replaceChildren();
      const layer = new TextLayer({
        textContentSource: proxy.streamTextContent(),
        container: textContainer,
        viewport,
      });
      textLayerRef.current = layer;
      await layer.render();
      if (cancelled) return;
      setTextVersion((value) => value + 1);
    })().catch((error: unknown) => {
      // 快速滚动时取消渲染是常态，不算错误
      const name = (error as { name?: string } | null)?.name;
      if (name !== 'RenderingCancelledException' && name !== 'AbortException') {
        console.warn(`[pdf] 第 ${page} 页渲染失败`, error);
      }
      if (!cancelled) setRendering(false);
    });

    return () => {
      cancelled = true;
      renderTask?.cancel();
      textLayerRef.current?.cancel();
    };
  }, [pdf, page, scale, active, onSize]);

  // 文字层画好（或目标切片变了）之后在这一页里找切片文字
  useEffect(() => {
    const layer = textLayerRef.current;
    const container = textRef.current;
    if (!container) return;
    container.querySelectorAll('.pdf-hl').forEach((element) => element.classList.remove('pdf-hl'));
    if (!layer || !highlight || textVersion === 0) return;

    const match = matchChunkInTextItems(layer.textContentItemsStr, highlight, { continuation });
    const divs = layer.textDivs;
    match?.indices.forEach((index) => divs[index]?.classList.add('pdf-hl'));
    onMatched(page, match ? (divs[match.indices[0]] ?? null) : null, highlightToken);
  }, [highlight, continuation, highlightToken, textVersion, page, onMatched]);

  const width = Math.floor(size.width * scale);
  const height = Math.floor(size.height * scale);

  return (
    <div
      ref={setRoot}
      data-page={page}
      className={cn(
        'pdf-page relative shrink-0 bg-white shadow-sm ring-1 ring-black/5',
        banner && 'ring-2 ring-primary',
      )}
      style={{ width, height, ['--total-scale-factor' as string]: scale }}
    >
      <canvas ref={canvasRef} className="absolute left-0 top-0" />
      <div ref={textRef} className="pdf-text-layer" />
      {banner}
      {active && rendering && (
        <div className="absolute inset-0 flex items-center justify-center text-xs text-neutral-400">
          <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />第 {page} 页
        </div>
      )}
      {!active && (
        <div className="absolute inset-0 flex items-center justify-center font-mono text-xs text-neutral-300">
          {page}
        </div>
      )}
    </div>
  );
});
