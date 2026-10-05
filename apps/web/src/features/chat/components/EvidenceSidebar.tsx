import React, { useRef, useEffect, useMemo, useState } from 'react';
import { formatShortcut } from '@/lib/platform';
import type { Insight, TraceDetail } from '@/lib/api/types.temp';
import { useUiStore } from '@/stores/useUiStore';
import { MarkdownView } from '@/components/shared/MarkdownView';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import { ScrollArea } from '@/components/ui/scroll-area';
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import {
  FileText,
  Lightbulb,
  Activity,
  X,
  Clock,
  Cpu,
  CornerDownRight,
  Sparkles,
  ExternalLink,
  GitFork,
  Layers,
  Search,
  ChevronDown,
  ChevronRight,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { toast } from 'sonner';
import { describeLocation, quoteRangeOf } from '../lib/citations';

interface EvidenceSidebarProps {
  compact?: boolean;
  retrievedChunks?: TraceDetail['retrieved'];
  /**
   * 这次回答真正引用了的切片。null 表示还说不清（生成中、模型尚未落引用），此时全列；
   * 空数组表示回答没有引用任何资料。
   */
  citedChunkIds?: string[] | null;
  /** 切片 id → 回答里的角标序号，见 features/chat/lib/citations。 */
  citationNumbers?: Map<string, number> | null;
  usedInsights?: TraceDetail['used_insights'];
  trace?: TraceDetail | null;
}

interface BatchMaxScores {
  maxVec: number;
  maxBm25: number;
  maxRrf: number;
  maxRerank: number;
}

function computeBatchMaxScores(chunks: TraceDetail['retrieved'] = []): BatchMaxScores {
  let maxVec = 0;
  let maxBm25 = 0;
  let maxRrf = 0;
  let maxRerank = 0;

  for (const c of chunks) {
    if (typeof c.vec_score === 'number' && c.vec_score > maxVec) maxVec = c.vec_score;
    if (typeof c.bm25_score === 'number' && c.bm25_score > maxBm25) maxBm25 = c.bm25_score;
    if (typeof c.rrf === 'number' && c.rrf > maxRrf) maxRrf = c.rrf;
    if (typeof c.rerank_score === 'number' && c.rerank_score > maxRerank) maxRerank = c.rerank_score;
  }

  return { maxVec, maxBm25, maxRrf, maxRerank };
}

/**
 * 召回路径徽章组件
 * 向量 (vector:*) / 全文 (fts:*) / 图谱 (graph)
 * 没有 legs 的老数据显示「未记录」，同一路多次命中只显示一次
 */
function RecallPathBadges({ legs }: { legs?: string[] }) {
  if (!legs || !Array.isArray(legs) || legs.length === 0) {
    return (
      <span
        className="inline-flex items-center rounded px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground bg-muted/50 border border-dashed border-border/80"
        title="该切片未记录召回路径（旧版数据）"
      >
        未记录
      </span>
    );
  }

  const hasVector = legs.some((l) => l.startsWith('vector'));
  const hasFts = legs.some((l) => l.startsWith('fts'));
  const hasGraph = legs.some((l) => l.startsWith('graph'));

  return (
    <div className="flex flex-wrap items-center gap-1">
      {hasVector && (
        <span
          className="inline-flex items-center gap-0.5 rounded px-1.5 py-0.5 font-mono text-[10px] font-medium border border-blue-500/30 bg-blue-500/10 text-blue-700 dark:text-blue-400"
          title="语义向量检索召回"
        >
          <Layers className="h-2.5 w-2.5" />
          <span>向量</span>
        </span>
      )}

      {hasFts && (
        <span
          className="inline-flex items-center gap-0.5 rounded px-1.5 py-0.5 font-mono text-[10px] font-medium border border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-400"
          title="BM25 关键词全文检索召回"
        >
          <Search className="h-2.5 w-2.5" />
          <span>全文</span>
        </span>
      )}

      {hasGraph && (
        <Tooltip>
          <TooltipTrigger>
            <span
              className="inline-flex items-center gap-0.5 rounded px-1.5 py-0.5 font-mono text-[10px] font-semibold border border-purple-500/40 bg-purple-500/15 text-purple-600 dark:text-purple-300 shadow-2xs ring-1 ring-purple-500/20 cursor-help"
            >
              <GitFork className="h-2.5 w-2.5" />
              <span>图谱扩展</span>
            </span>
          </TooltipTrigger>
          <TooltipContent side="top" className="text-xs max-w-xs">
            由知识图谱相邻实体扩展而来
          </TooltipContent>
        </Tooltip>
      )}
    </div>
  );
}

/**
 * 横向对比分数条组件
 * 各路按本批最大值归一化显示相对长度，缺失的留空而不是画成 0
 */
function EvidenceScoreBars({
  chunk,
  batchMaxScores,
}: {
  chunk: NonNullable<TraceDetail['retrieved']>[number];
  batchMaxScores: BatchMaxScores;
}) {
  const bars: Array<{
    key: string;
    label: string;
    raw: number;
    max: number;
    ratio: number;
    displayVal: string;
    color: string;
    desc: string;
  }> = [];

  /**
   * 格式化召回分，永不把非零分显示成 0。
   *
   * BM25 此前用 `toFixed(1)`，而同质语料里 bm25 常常小到 1e-06 量级
   * （比如本库 90 条切片有 73 条都含 "hERG"，IDF 趋近 0 是 BM25 的正确行为），
   * 于是每条证据都写着「全文 0.0」，读起来像「全文这一路什么都没召回」——
   * 可旁边的召回路径徽章明明标着 fts 命中。这是调试面板，
   * 「极小但非零」和「真的为零」含义完全不同，不能糊成一个数。
   */
  const formatScore = (value: number, digits: number): string => {
    if (value === 0) return '0';
    return Math.abs(value) >= 10 ** -digits
      ? value.toFixed(digits)
      : value.toExponential(1);
  };

  if (typeof chunk.vec_score === 'number') {
    const max = batchMaxScores.maxVec || 1;
    bars.push({
      key: 'vec',
      label: '向量',
      raw: chunk.vec_score,
      max,
      ratio: Math.min(100, Math.max(8, (chunk.vec_score / max) * 100)),
      displayVal: formatScore(chunk.vec_score, 2),
      color: 'bg-blue-500/80 dark:bg-blue-400/80',
      desc: '向量相似度分',
    });
  }

  if (typeof chunk.bm25_score === 'number') {
    const max = batchMaxScores.maxBm25 || 1;
    bars.push({
      key: 'bm25',
      label: '全文',
      raw: chunk.bm25_score,
      max,
      ratio: Math.min(100, Math.max(8, (chunk.bm25_score / max) * 100)),
      displayVal: formatScore(chunk.bm25_score, 2),
      color: 'bg-amber-500/80 dark:bg-amber-400/80',
      desc: 'BM25 全文词频分',
    });
  }

  if (typeof chunk.rrf === 'number') {
    const max = batchMaxScores.maxRrf || 1;
    bars.push({
      key: 'rrf',
      label: 'RRF',
      raw: chunk.rrf,
      max,
      ratio: Math.min(100, Math.max(8, (chunk.rrf / max) * 100)),
      displayVal: formatScore(chunk.rrf, 3),
      color: 'bg-cyan-500/80 dark:bg-cyan-400/80',
      desc: '倒数排名融合分 (RRF)',
    });
  }

  if (typeof chunk.rerank_score === 'number') {
    const max = batchMaxScores.maxRerank || 1;
    bars.push({
      key: 'rerank',
      label: '重排',
      raw: chunk.rerank_score,
      max,
      ratio: Math.min(100, Math.max(8, (chunk.rerank_score / max) * 100)),
      displayVal: formatScore(chunk.rerank_score, 2),
      color: 'bg-emerald-500/80 dark:bg-emerald-400/80',
      desc: '交叉重排模型分',
    });
  }

  if (bars.length === 0) return null;

  return (
    <div className="space-y-1 pt-1">
      {bars.map((b) => (
        <Tooltip key={b.key}>
          <TooltipTrigger className="w-full text-left cursor-help">
            <div className="flex items-center gap-1.5 text-[10px]">
              <span className="w-6 shrink-0 font-medium text-muted-foreground">{b.label}</span>
              <div className="flex-1 h-1.5 rounded-full bg-muted/60 overflow-hidden relative">
                <div
                  className={cn('h-full rounded-full transition-all duration-300', b.color)}
                  style={{ width: `${b.ratio}%` }}
                />
              </div>
              {/* w-12 而非 w-8：科学计数法（如 1.7e-6）比两位小数宽 */}
              <span className="font-mono text-muted-foreground tabular-nums w-12 shrink-0 text-right">
                {b.displayVal}
              </span>
            </div>
          </TooltipTrigger>
          <TooltipContent side="top" className="text-xs max-w-xs">
            <div className="font-medium text-foreground">{b.desc}</div>
            <div className="text-muted-foreground text-[11px] mt-0.5">
              原始分值：<span className="font-mono font-semibold text-foreground">{b.raw}</span>
              {' · '}
              本批最高：<span className="font-mono text-foreground">{b.max}</span>
              {' · '}
              相对占比：<span className="font-mono text-foreground">{Math.round(b.ratio)}%</span>
            </div>
          </TooltipContent>
        </Tooltip>
      ))}
    </div>
  );
}

function ChunkCard({
  chunk,
  index,
  isHighlighted,
  batchMaxScores,
  isCited = true,
  citedNumber = null,
}: {
  chunk: NonNullable<TraceDetail['retrieved']>[number];
  index: number;
  isHighlighted: boolean;
  batchMaxScores: BatchMaxScores;
  /** 没被回答引用的只是「检索到了」，不能也标成「引用 N」 */
  isCited?: boolean;
  /** 回答里这条证据的角标序号（按正文出现顺序编号，与回答一致）。 */
  citedNumber?: number | null;
}) {
  const cardRef = useRef<HTMLDivElement>(null);
  const { openReader, highlightRequestSeq, showRetrievalScores } = useUiStore();

  // 依赖里带上请求计数：同一条证据被连点两次时 highlightedChunkId 没变，只看
  // isHighlighted 的话 effect 不会再跑，用户点了「定位」却什么都不发生。
  useEffect(() => {
    if (isHighlighted && cardRef.current) {
      cardRef.current.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  }, [isHighlighted, highlightRequestSeq]);

  const isSummary = chunk.kind === 'summary';

  const handleOpenReader = (e?: React.MouseEvent) => {
    e?.stopPropagation();
    if (chunk.document_id) {
      openReader({
        documentId: chunk.document_id,
        documentTitle: chunk.document_title || '文档',
        chunkId: isSummary ? undefined : chunk.chunk_id,
        quote: quoteRangeOf(chunk),
      });
    } else {
      toast.info('该切片未关联文档 ID，无法在全文中定位');
    }
  };

  return (
    <div
      ref={cardRef}
      id={`evidence-chunk-${chunk.chunk_id}`}
      onClick={() => handleOpenReader()}
      className={cn(
        'group rounded-lg border bg-card p-3 text-xs shadow-2xs transition-all duration-200 cursor-pointer space-y-2',
        isHighlighted
          ? 'border-primary ring-2 ring-primary/40 bg-primary/10 shadow-sm'
          : 'border-border/70 hover:border-border hover:shadow-xs',
      )}
    >
      {/* 顶部索引、路径徽章与操作 */}
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 flex-wrap">
          {/* 序号与回答里的引用角标一致：角标按正文出现顺序编号，这里用同一套号 */}
          <span
            className={cn(
              'font-mono text-[10px] font-semibold',
              isCited ? 'text-primary' : 'text-muted-foreground',
            )}
          >
            {isCited ? `引用 ${citedNumber ?? index + 1}` : `检索第 ${index + 1}`}
          </span>
          {isSummary && (
            <span className="rounded bg-primary/15 text-primary border border-primary/30 px-1.5 py-0.2 font-medium text-[10px]">
              文档概要
            </span>
          )}
          {/* 合并过的证据要说清楚：它是同一节里连着的几段并起来的，不是某一段被撑长了 */}
          {(chunk.merged_from?.length ?? 0) > 1 && (
            <span
              className="rounded bg-muted text-muted-foreground border border-border/70 px-1.5 py-0.2 font-medium text-[10px]"
              title="同一节里连号的切片已合并为一条证据，引用仍指向其中命中的那一段"
            >
              合并 {chunk.merged_from?.length} 段
            </span>
          )}
          <RecallPathBadges legs={chunk.legs} />
        </div>

        <div className="flex items-center gap-1.5 text-[10px] text-muted-foreground shrink-0">
          <button
            type="button"
            onClick={handleOpenReader}
            className="flex items-center gap-0.5 text-primary hover:underline font-medium text-[10px] cursor-pointer"
            title="在阅读器中定位原文切片"
          >
            <span>查看原文</span>
            <ExternalLink className="h-3 w-3" />
          </button>
        </div>
      </div>

      {/* 标题 */}
      <div className="min-w-0">
        <div className="font-medium text-foreground line-clamp-1 text-xs">
          {chunk.document_title || '未命名文档'}
        </div>
        {describeLocation(chunk) && (
          <div className="mt-0.5 line-clamp-1 text-[11px] text-muted-foreground">
            {describeLocation(chunk)}
          </div>
        )}
      </div>

      {/* 摘录正文：片段来自原文，常含表格与列表，按 Markdown 渲染并限高 */}
      {/* 摘录可能从一个 `# 标题` 开始：标题压成正文字号，否则一行标题就占满整张卡片 */}
      <div className="max-h-24 overflow-hidden text-muted-foreground leading-relaxed text-[11px] prose prose-sm dark:prose-invert max-w-none prose-p:my-0.5 prose-table:my-0.5 prose-pre:my-0.5 [&_:is(h1,h2,h3,h4,h5,h6)]:my-0.5! [&_:is(h1,h2,h3,h4,h5,h6)]:text-[11px]! [&_:is(h1,h2,h3,h4,h5,h6)]:font-semibold! [&_:is(h1,h2,h3,h4,h5,h6)]:text-foreground">
        <MarkdownView>{chunk.snippet ?? ''}</MarkdownView>
      </div>

      {/* 各路分数横向对比条：调参才看，默认收起（执行轨迹页签里始终有） */}
      {showRetrievalScores && (
        <div className="border-t border-border/40 pt-1.5">
          <EvidenceScoreBars chunk={chunk} batchMaxScores={batchMaxScores} />
        </div>
      )}
    </div>
  );
}

function EvidenceEmptyState({
  icon: Icon,
  title,
  desc,
}: {
  icon: React.ElementType;
  title: string;
  desc: string;
}) {
  return (
    <div className="flex h-64 flex-col items-center justify-center p-6 text-center">
      <div className="flex h-10 w-10 items-center justify-center rounded-full bg-muted/80 text-muted-foreground mb-3 shadow-2xs">
        <Icon className="h-5 w-5" />
      </div>
      <div className="text-xs font-semibold text-foreground">{title}</div>
      <p className="mt-1.5 text-[11px] text-muted-foreground leading-relaxed max-w-[220px]">
        {desc}
      </p>
    </div>
  );
}

export function EvidenceSidebar({
  compact = false,
  retrievedChunks = [],
  citedChunkIds = null,
  citationNumbers = null,
  usedInsights = [],
  trace = null,
}: EvidenceSidebarProps) {
  const {
    isEvidenceOpen,
    toggleEvidence,
    activeEvidenceTab,
    setActiveEvidenceTab,
    highlightedChunkId,
    openReader,
    showRetrievalScores,
    toggleRetrievalScores,
  } = useUiStore();

  const [showUncited, setShowUncited] = useState(false);

  // 保留检索序号：卡片上的「引用 N」要和回答里的角标对得上
  const { citedEntries, uncitedEntries } = useMemo(() => {
    const entries = retrievedChunks.map((chunk, index) => ({ chunk, index }));
    if (citedChunkIds === null)
      return { citedEntries: entries.map((e) => ({ ...e, n: null as number | null })), uncitedEntries: [] };
    const cited = new Set(citedChunkIds);
    // 合并过的证据，引用可能指向其中任一段
    const isCited = (chunk: (typeof retrievedChunks)[number]) =>
      cited.has(chunk.chunk_id) || (chunk.merged_from ?? []).some((id) => cited.has(id));
    const numberOf = (chunk: (typeof retrievedChunks)[number]) => {
      if (!citationNumbers) return null;
      const direct = citationNumbers.get(chunk.chunk_id);
      if (direct !== undefined) return direct;
      for (const id of chunk.merged_from ?? []) {
        const n = citationNumbers.get(id);
        if (n !== undefined) return n;
      }
      return null;
    };
    return {
      citedEntries: entries
        .filter((e) => isCited(e.chunk))
        .map((e) => ({ ...e, n: numberOf(e.chunk) }))
        .sort((a, b) => (a.n ?? 1e9) - (b.n ?? 1e9)),
      uncitedEntries: entries.filter((e) => !isCited(e.chunk)),
    };
  }, [retrievedChunks, citedChunkIds, citationNumbers]);

  // 换了一条回答就收起「未引用」，不把上一条的展开状态带过去
  useEffect(() => {
    setShowUncited(false);
  }, [trace?.id]);

  // 证据栏里要被「定位」的切片若在未引用那一组，自动展开，否则定位点下去什么都看不到
  useEffect(() => {
    if (highlightedChunkId && uncitedEntries.some((e) => e.chunk.chunk_id === highlightedChunkId)) {
      setShowUncited(true);
    }
  }, [highlightedChunkId, uncitedEntries]);

  const chunksBatchMaxScores = useMemo(
    () => computeBatchMaxScores(retrievedChunks),
    [retrievedChunks],
  );

  const traceBatchMaxScores = useMemo(
    () => computeBatchMaxScores(trace?.retrieved || []),
    [trace?.retrieved],
  );

  if (!isEvidenceOpen) return null;

  return (
    <aside aria-label="引用来源" className={cn("flex h-full min-h-0 shrink-0 flex-col border-l border-border bg-card/60 select-none", compact ? "w-full" : "w-[336px]")}>
      <Tabs
        value={activeEvidenceTab}
        onValueChange={(val) => setActiveEvidenceTab(val as 'chunks' | 'insights' | 'trace')}
        className="flex h-full flex-col"
      >
        {/* 顶部 Tab 栏与收起按钮 */}
        <div className="flex h-12 items-center justify-between border-b border-border px-3 shrink-0">
          <TabsList className="bg-muted/60 p-1">
            <TabsTrigger value="chunks" className="gap-1.5 text-xs">
              <FileText className="h-3.5 w-3.5" />
              <span>资料 ({citedEntries.length})</span>
            </TabsTrigger>
            <TabsTrigger value="insights" className="gap-1.5 text-xs">
              <Lightbulb className="h-3.5 w-3.5 text-accent-insight" />
              <span>经验 ({usedInsights.length})</span>
            </TabsTrigger>
            <TabsTrigger value="trace" className="gap-1.5 text-xs">
              <Activity className="h-3.5 w-3.5 text-accent-ai" />
              <span>执行轨迹</span>
            </TabsTrigger>
          </TabsList>

          <button
            type="button"
            onClick={toggleEvidence}
            className="rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-foreground transition-colors cursor-pointer"
            title={`收起证据栏 (${formatShortcut('⌘\\')})`}
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <TabsContent value="chunks" className="flex-1 overflow-hidden m-0 p-0">
          <ScrollArea className="h-[calc(100vh-100px)] px-3 py-2">
            {retrievedChunks.length === 0 ? (
              <EvidenceEmptyState
                icon={FileText}
                title="暂无检索资料"
                desc="提问后，这里会列出经向量检索、全文匹配与重排序后找到的资料切片。"
              />
            ) : (
              <div className="space-y-2.5 py-1">
                <div className="flex justify-end">
                  <button
                    type="button"
                    onClick={toggleRetrievalScores}
                    aria-pressed={showRetrievalScores}
                    className="text-[10px] text-muted-foreground hover:text-foreground transition-colors cursor-pointer"
                    title="各路召回与重排的分数，调检索参数时有用"
                  >
                    {showRetrievalScores ? '隐藏检索分数' : '显示检索分数'}
                  </button>
                </div>
                {citedEntries.length === 0 && (
                  <div className="rounded-lg border border-dashed border-border/80 px-3 py-4 text-center text-[11px] text-muted-foreground">
                    这次回答没有引用知识库里的资料
                  </div>
                )}
                {citedEntries.map(({ chunk, index, n }) => (
                  <ChunkCard
                    key={chunk.chunk_id || index}
                    chunk={chunk}
                    index={index}
                    citedNumber={n}
                    isHighlighted={
                      highlightedChunkId === chunk.chunk_id ||
                      (!!highlightedChunkId && (chunk.merged_from ?? []).includes(highlightedChunkId))
                    }
                    batchMaxScores={chunksBatchMaxScores}
                  />
                ))}

                {uncitedEntries.length > 0 && (
                  <>
                    <button
                      type="button"
                      onClick={() => setShowUncited((v) => !v)}
                      aria-expanded={showUncited}
                      className="flex w-full items-center gap-1 rounded-md px-1 py-1.5 text-[11px] text-muted-foreground hover:text-foreground transition-colors cursor-pointer"
                    >
                      {showUncited ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
                      <span>检索到但未引用 {uncitedEntries.length} 条</span>
                    </button>
                    {showUncited &&
                      uncitedEntries.map(({ chunk, index }) => (
                        <ChunkCard
                          key={chunk.chunk_id || index}
                          chunk={chunk}
                          index={index}
                          isCited={false}
                          isHighlighted={highlightedChunkId === chunk.chunk_id}
                          batchMaxScores={chunksBatchMaxScores}
                        />
                      ))}
                  </>
                )}
              </div>
            )}
          </ScrollArea>
        </TabsContent>

        <TabsContent value="insights" className="flex-1 overflow-hidden m-0 p-0">
          <ScrollArea className="h-[calc(100vh-100px)] px-3 py-2">
            {usedInsights.length === 0 ? (
              <EvidenceEmptyState
                icon={Lightbulb}
                title="暂无生效经验"
                desc="问题命中以往从反馈中沉淀的经验时，这里会列出本次参考了哪些经验。"
              />
            ) : (
              <div className="space-y-2.5 py-1">
                {usedInsights.map((item, idx) => {
                  const isObj = typeof item === 'object' && item !== null;
                  const insight = isObj ? (item as Insight) : null;
                  // 只剩 id：这条经验在回答之后被删了。如实说，不拿 id 当条件、不编对策
                  if (!insight) {
                    return (
                      <div
                        key={typeof item === 'string' ? item : idx}
                        className="rounded-lg border border-dashed border-border/80 p-3 text-[11px] text-muted-foreground"
                      >
                        这条经验在回答之后已被删除，内容无法显示。
                      </div>
                    );
                  }
                  const condition = insight.trigger;
                  const guidance = insight.guidance;
                  // 置信度是经验晋升/淘汰的依据，取不到就不显示，
                  // 不能兜底成 0.85——那是凭空造一个「挺可信」的数
                  const confidence = insight?.confidence ?? null;
                  const scopeBadge = insight.scope === 'global' ? '全局领域经验' : '空间专有经验';

                  return (
                    <div
                      key={insight?.id || idx}
                      className="rounded-lg border border-accent-insight/25 bg-accent-insight/5 p-3 text-xs shadow-2xs"
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="rounded bg-accent-insight/15 px-1.5 py-0.5 font-mono text-[10px] font-semibold text-accent-insight">
                          {scopeBadge}
                        </span>
                        {confidence !== null && (
                          <div className="flex items-center gap-1 text-[10px] text-muted-foreground">
                            <span>置信度</span>
                            <span className="font-mono font-medium text-foreground">
                              {Math.round(confidence * 100)}%
                            </span>
                          </div>
                        )}
                      </div>
                      <div className="mt-2 font-medium text-foreground text-xs leading-snug">
                        [条件] {condition}
                      </div>
                      <div className="mt-1 text-muted-foreground text-[11px] leading-relaxed">
                        [对策] {guidance}
                      </div>
                      {/* 这里原本有一对 👍/👎「反馈效果」按钮，点了只弹一句
                          「已记录正向反馈」/「已记录负向反馈，下次进化时优化」，
                          却不发任何请求——后端根本没有单条经验的反馈端点。
                          而消息下方的「满意 / 提出问题 / 纠错」是真的，并且会把反馈
                          归因到本次注入的每一条经验（日志里的 insight_feedback_attributed）。
                          所以这对按钮既是假的又是冗余的，直接撤掉，反馈统一走消息级。 */}
                    </div>
                  );
                })}
              </div>
            )}
          </ScrollArea>
        </TabsContent>

        <TabsContent value="trace" className="flex-1 overflow-hidden m-0 p-0">
          <ScrollArea className="h-[calc(100vh-100px)] px-3 py-2">
            {!trace ? (
              <EvidenceEmptyState
                icon={Activity}
                title="暂无执行轨迹"
                desc="发送消息后将在此追踪查询重写、各路证据召回详情与模型消耗细节。"
              />
            ) : (
              <div className="space-y-3 py-1">
                {/* 结构化轨迹看板 */}
                <div className="rounded-lg border border-border/70 bg-card p-3 text-xs shadow-2xs space-y-3">
                  <div className="flex items-center justify-between text-[11px] text-muted-foreground border-b border-border/50 pb-2">
                    <span className="font-mono text-[10px]">Trace: {trace.id?.slice(0, 8)}</span>
                    <span className="flex items-center gap-1 text-foreground font-mono">
                      <Clock className="h-3 w-3 text-primary" />
                      {/* 没有真实耗时就说没有。原本兜底成 '0.8s'——凭空编一个
                          看着很合理的耗时，比留白误导得多 */}
                      {trace.latency_ms ? `${(trace.latency_ms / 1000).toFixed(2)}s` : '—'}
                    </span>
                  </div>

                  {/* 意图重写 */}
                  <div className="space-y-1">
                    <div className="text-[11px] font-medium text-muted-foreground flex items-center gap-1.5">
                      <CornerDownRight className="h-3 w-3 text-primary" />
                      <span>查询改写</span>
                    </div>
                    <div className="rounded bg-muted/40 p-2 text-[11px] text-foreground font-mono leading-relaxed break-words">
                      {/* 没改写时别把原问题填进来冒充改写结果 */}
                      {trace.rewritten_query && trace.rewritten_query !== trace.query
                        ? trace.rewritten_query
                        : '未改写，直接用原问题检索'}
                    </div>
                  </div>

                  {/* 模型与 Token 消耗 */}
                  <div className="grid grid-cols-2 gap-2 pt-1">
                    <div className="rounded border border-border/60 bg-muted/20 p-2">
                      <div className="flex items-center gap-1 text-[10px] text-muted-foreground">
                        <Cpu className="h-3 w-3 text-accent-ai" />
                        <span>生成模型</span>
                      </div>
                      <div className="mt-1 font-mono text-[11px] font-medium text-foreground truncate">
                        {trace.model || '未记录'}
                      </div>
                    </div>
                    <div className="rounded border border-border/60 bg-muted/20 p-2">
                      <div className="flex items-center gap-1 text-[10px] text-muted-foreground">
                        <Sparkles className="h-3 w-3 text-primary" />
                        <span>词元消耗</span>
                      </div>
                      <div className="mt-1 font-mono text-[11px] font-medium text-foreground">
                        {(trace.prompt_tokens || 0) + (trace.completion_tokens || 0)}
                      </div>
                    </div>
                  </div>

                  {/* 检索参数与阶段详情 */}
                  <div className="border-t border-border/40 pt-2 space-y-1.5 text-[11px]">
                    <div className="flex justify-between text-muted-foreground">
                      <span>召回证据数</span>
                      <span className="font-mono text-foreground">{trace.retrieved?.length || 0}</span>
                    </div>
                    <div className="flex justify-between text-muted-foreground">
                      <span>注入对策数</span>
                      <span className="font-mono text-accent-insight">{trace.used_insights?.length || 0}</span>
                    </div>
                    {trace.latency_ms ? (
                      <div className="flex justify-between text-muted-foreground">
                        <span>端到端延迟</span>
                        <span className="font-mono text-foreground">{trace.latency_ms}ms</span>
                      </div>
                    ) : null}
                  </div>
                </div>

                {/* 轨迹面板里的证据列表及召回分析 */}
                <div className="rounded-lg border border-border/70 bg-card p-3 text-xs shadow-2xs space-y-3">
                  <div className="flex items-center justify-between border-b border-border/50 pb-2">
                    <div className="flex items-center gap-1.5 font-medium text-foreground text-xs">
                      <FileText className="h-3.5 w-3.5 text-primary" />
                      <span>检索证据与多路召回 ({trace.retrieved?.length || 0})</span>
                    </div>
                    <span className="text-[10px] text-muted-foreground">各路归一化</span>
                  </div>

                  {(!trace.retrieved || trace.retrieved.length === 0) ? (
                    <div className="py-4 text-center text-muted-foreground text-[11px]">
                      当前轨迹无召回切片记录
                    </div>
                  ) : (
                    <div className="space-y-2.5">
                      {trace.retrieved.map((chunk, index) => {
                        const isSummary = chunk.kind === 'summary';
                        return (
                          <div
                            key={chunk.chunk_id || index}
                            onClick={() => {
                              if (chunk.document_id) {
                                openReader({
                                  documentId: chunk.document_id,
                                  documentTitle: chunk.document_title || '文档',
                                  chunkId: isSummary ? undefined : chunk.chunk_id,
                                  quote: quoteRangeOf(chunk),
                                });
                              }
                            }}
                            className="rounded-md border border-border/60 bg-muted/20 p-2.5 text-xs space-y-1.5 hover:border-border transition-colors cursor-pointer"
                          >
                            <div className="flex items-center justify-between gap-2">
                              <div className="flex items-center gap-1.5 flex-wrap">
                                <span className="font-mono text-[10px] font-semibold text-primary">
                                  [#{index + 1}]
                                </span>
                                {isSummary && (
                                  <span className="rounded bg-primary/15 text-primary border border-primary/30 px-1.5 py-0.2 font-medium text-[10px]">
                                    文档概要
                                  </span>
                                )}
                                <RecallPathBadges legs={chunk.legs} />
                              </div>
                            <span className="font-mono text-[10px] text-muted-foreground truncate max-w-[120px]">
                              {chunk.chunk_id?.slice(0, 10)}
                            </span>
                          </div>

                          <div className="font-medium text-foreground text-[11px] truncate">
                            {chunk.document_title || '未命名文档'}
                            {chunk.page ? ` · P.${chunk.page}` : ''}
                          </div>

                          {chunk.snippet && (
                            <div className="max-h-20 overflow-hidden text-muted-foreground text-[10.5px] leading-relaxed prose prose-sm dark:prose-invert max-w-none prose-p:my-0.5 prose-table:my-0.5 prose-pre:my-0.5 [&_:is(h1,h2,h3,h4,h5,h6)]:my-0.5! [&_:is(h1,h2,h3,h4,h5,h6)]:text-[10.5px]! [&_:is(h1,h2,h3,h4,h5,h6)]:font-semibold! [&_:is(h1,h2,h3,h4,h5,h6)]:text-foreground">
                              <MarkdownView>{chunk.snippet ?? ''}</MarkdownView>
                            </div>
                          )}

                          <EvidenceScoreBars
                            chunk={chunk}
                            batchMaxScores={traceBatchMaxScores}
                          />
                        </div>
                        );
                      })}
                    </div>
                  )}
                </div>
              </div>
            )}
          </ScrollArea>
        </TabsContent>
      </Tabs>
    </aside>
  );
}
