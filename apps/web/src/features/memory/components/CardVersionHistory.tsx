import React, { useState, useEffect, useCallback } from 'react';
import type { KnowledgeCard, CardVersion, CardHistoryResponse } from '@/lib/api/types.temp';
import { memoryService } from '@/lib/api/services/memory';
import { MarkdownView } from '@/components/shared/MarkdownView';
import { ConfidenceRing } from '@/components/shared/ConfidenceRing';
import { Button } from '@/components/ui/button';
import {
  History,
  Clock,
  BadgeCheck,
  Sparkles,
  HelpCircle,
  Info,
  ArrowRight,
  RotateCw,
  Columns2,
  Rows2,
  CheckCircle2,
} from 'lucide-react';

interface CardVersionHistoryProps {
  card: KnowledgeCard;
  spaceId?: string;
  onRefresh?: () => void;
}

interface VersionTimelineItem {
  id: string;
  version: number;
  isCurrent: boolean;
  title: string;
  body: string;
  confidence: number;
  verified_by?: ('user' | 'eval') | null;
  valid_from: number;
  valid_to?: number | null;
  previous?: CardVersion | null;
}

function formatTime(ts?: number | null): string {
  if (!ts) return '-';
  const d = new Date(ts);
  const year = d.getFullYear();
  const month = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  const hours = String(d.getHours()).padStart(2, '0');
  const minutes = String(d.getMinutes()).padStart(2, '0');
  return `${year}-${month}-${day} ${hours}:${minutes}`;
}

export function CardVersionHistory({
  card,
  spaceId,
  onRefresh,
}: CardVersionHistoryProps) {
  const [historyData, setHistoryData] = useState<CardHistoryResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [layoutMode, setLayoutMode] = useState<'split' | 'stacked'>('split');

  const targetSpaceId = spaceId || card.space_id;

  const fetchVersions = useCallback(async () => {
    if (!targetSpaceId || !card.id) return;
    setLoading(true);
    setError(null);
    try {
      const res = await memoryService.getCardVersions(targetSpaceId, card.id);
      setHistoryData(res);
    } catch {
      setError('无法获取卡片版本历史，请检查网络或后端服务状态。');
    } finally {
      setLoading(false);
    }
  }, [targetSpaceId, card.id]);

  useEffect(() => {
    fetchVersions();
  }, [fetchVersions]);

  const handleManualRefresh = async () => {
    await fetchVersions();
    onRefresh?.();
  };

  const currentCard = historyData?.card || card;
  const versions: CardVersion[] = historyData?.versions || [];

  // 计算版本时间线（新的在前）
  const timelineItems: VersionTimelineItem[] = [];
  if (versions.length > 0) {
    const currentVersionNumber = versions[0].version + 1;
    // 1. 当前版本（生效中）
    timelineItems.push({
      id: `curr-${currentCard.id}`,
      version: currentVersionNumber,
      isCurrent: true,
      title: currentCard.title,
      body: currentCard.body,
      confidence: currentCard.confidence,
      verified_by: currentCard.verified_by,
      valid_from: currentCard.updated_at || currentCard.created_at,
      valid_to: null,
      previous: versions[0],
    });

    // 2. 历史版本（时间倒序）
    for (let i = 0; i < versions.length; i++) {
      const v = versions[i];
      const prev = i + 1 < versions.length ? versions[i + 1] : null;
      timelineItems.push({
        id: v.id,
        version: v.version,
        isCurrent: false,
        title: v.title,
        body: v.body,
        confidence: v.confidence,
        verified_by: v.verified_by,
        valid_from: v.valid_from,
        valid_to: v.valid_to,
        previous: prev,
      });
    }
  }

  const renderVerificationBadge = (verifiedBy?: ('user' | 'eval') | null) => {
    if (verifiedBy === 'user') {
      return (
        <span
          className="inline-flex items-center gap-1 rounded-md bg-accent-insight/10 px-2 py-0.5 text-[10px] font-medium text-accent-insight border border-accent-insight/25"
          title="已人工校验，在混合检索与模型生成时享有最高优先级，且不会被抽取覆盖"
        >
          <BadgeCheck className="h-3 w-3" />
          人工核验
        </span>
      );
    }
    if (verifiedBy === 'eval') {
      return (
        <span
          className="inline-flex items-center gap-1 rounded-md bg-accent-ai/10 px-2 py-0.5 text-[10px] font-medium text-accent-ai border border-accent-ai/25"
          title="经自动化评测基准验证"
        >
          <Sparkles className="h-3 w-3" />
          评测核验
        </span>
      );
    }
    return (
      <span
        className="inline-flex items-center gap-1 rounded-md bg-muted/50 px-2 py-0.5 text-[10px] font-normal text-muted-foreground border border-border/40"
        title="模型或规则自动抽取，尚未人工校验"
      >
        <HelpCircle className="h-3 w-3" />
        自动抽取
      </span>
    );
  };

  return (
    <div className="space-y-4">
      {/* 留档规则说明条（准确阐述后端事实留档机制） */}
      <div className="rounded-lg border border-border/70 bg-muted/20 p-3 text-xs leading-relaxed text-muted-foreground space-y-1.5">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-1.5 font-medium text-foreground text-[11px]">
            <Info className="h-3.5 w-3.5 text-primary" />
            <span>知识事实留档规则</span>
          </div>
          <div className="flex items-center gap-2">
            <div className="inline-flex items-center rounded-md border border-border/60 bg-background/80 p-0.5 text-[10px]">
              <button
                type="button"
                onClick={() => setLayoutMode('split')}
                className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded cursor-pointer transition-colors ${
                  layoutMode === 'split' ? 'bg-muted text-foreground font-medium' : 'text-muted-foreground hover:text-foreground'
                }`}
                title="并排对照"
              >
                <Columns2 className="h-3 w-3" />
                <span>并排</span>
              </button>
              <button
                type="button"
                onClick={() => setLayoutMode('stacked')}
                className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded cursor-pointer transition-colors ${
                  layoutMode === 'stacked' ? 'bg-muted text-foreground font-medium' : 'text-muted-foreground hover:text-foreground'
                }`}
                title="上下对照"
              >
                <Rows2 className="h-3 w-3" />
                <span>上下</span>
              </button>
            </div>
            <Button
              variant="ghost"
              size="icon-xs"
              onClick={handleManualRefresh}
              disabled={loading}
              title="刷新版本历史"
            >
              <RotateCw className={`h-3 w-3 ${loading ? 'animate-spin' : ''}`} />
            </Button>
          </div>
        </div>
        <p className="text-[11px] text-muted-foreground pl-5">
          只有<strong>正文或标题被改动</strong>才产生历史版本（人工编辑、或自动抽取用更高置信度的内容覆盖时）。只改别名不算「事实变了」；已人工核验（<code>verified_by: &quot;user&quot;</code>）的卡片不会被自动抽取覆盖，因此也不会产生自动历史。
        </p>
      </div>

      {/* 加载中状态 */}
      {loading && !historyData && (
        <div className="flex flex-col items-center justify-center py-12 text-center text-muted-foreground space-y-2">
          <RotateCw className="h-5 w-5 animate-spin text-primary" />
          <span className="text-xs">正在拉取版本留档历史...</span>
        </div>
      )}

      {/* 错误状态 */}
      {error && !loading && (
        <div className="rounded-lg border border-destructive/30 bg-destructive/10 p-4 text-center space-y-2">
          <p className="text-xs text-destructive">{error}</p>
          <Button variant="outline" size="sm" onClick={fetchVersions} className="text-xs">
            重新尝试
          </Button>
        </div>
      )}

      {/* 无历史版本时的友好展示 */}
      {!loading && !error && versions.length === 0 && (
        <div className="space-y-3">
          <div className="rounded-xl border border-dashed border-border/80 bg-muted/15 p-6 text-center space-y-2">
            <History className="h-8 w-8 mx-auto text-muted-foreground" />
            <h4 className="text-sm font-semibold text-foreground">尚无历史版本</h4>
            <p className="text-xs text-muted-foreground max-w-md mx-auto leading-relaxed">
              这张卡片自创建以来没有被改动过。
            </p>
            <div className="pt-2">
              <span className="inline-flex items-center gap-1.5 text-[11px] text-muted-foreground bg-muted/40 px-2.5 py-1 rounded-md border border-border/50">
                <CheckCircle2 className="h-3.5 w-3.5 text-emerald-700 dark:text-emerald-400" />
                当前为初始版本 (v1) · 生效于 {formatTime(currentCard.created_at)}
              </span>
            </div>
          </div>

          {/* 当前版本基本信息快照 */}
          <div className="rounded-lg border border-border/60 bg-card p-4 space-y-3 text-xs">
            <div className="flex items-center justify-between border-b border-border/40 pb-2.5">
              <div className="flex items-center gap-2">
                <span className="rounded bg-primary/10 px-2 py-0.5 text-[11px] font-mono font-semibold text-primary">
                  v1 (当前生效)
                </span>
                <span className="font-mono text-[11px] text-muted-foreground uppercase">
                  [{currentCard.kind}]
                </span>
              </div>
              <div className="flex items-center gap-2">
                {renderVerificationBadge(currentCard.verified_by)}
                <div className="flex items-center gap-1 text-[11px] text-muted-foreground">
                  <span>置信度</span>
                  <ConfidenceRing value={currentCard.confidence} size={20} strokeWidth={2.5} />
                </div>
              </div>
            </div>
            <div>
              <span className="text-[11px] text-muted-foreground">卡片标题：</span>
              <p className="font-medium text-foreground mt-0.5">{currentCard.title}</p>
            </div>
            <div>
              <span className="text-[11px] text-muted-foreground">卡片正文：</span>
              <pre className="mt-1 max-h-48 overflow-y-auto rounded-md border border-border/50 bg-muted/20 p-2.5 font-mono text-[11px] text-foreground leading-relaxed whitespace-pre-wrap">
                <MarkdownView>{currentCard.body}</MarkdownView>
              </pre>
            </div>
          </div>
        </div>
      )}

      {/* 历史版本列表（时间倒序，含当前版本与历史版本对比） */}
      {!loading && !error && timelineItems.length > 0 && (
        <div className="space-y-4">
          {timelineItems.map((item) => {
            const hasPrev = Boolean(item.previous);
            const titleChanged = hasPrev && item.previous?.title !== item.title;

            return (
              <div
                key={item.id}
                className={`rounded-xl border transition-all duration-200 overflow-hidden ${
                  item.isCurrent
                    ? 'border-emerald-500/40 bg-card shadow-xs'
                    : 'border-border/70 bg-card/60'
                }`}
              >
                {/* 单版头部：版本号、生效起止时间、核验来源、置信度 */}
                <div
                  className={`p-3.5 border-b flex flex-wrap items-center justify-between gap-2.5 text-xs ${
                    item.isCurrent
                      ? 'bg-emerald-500/5 border-emerald-500/20'
                      : 'bg-muted/30 border-border/50'
                  }`}
                >
                  <div className="flex items-center gap-2">
                    <span
                      className={`inline-flex items-center gap-1 rounded px-2 py-0.5 text-[11px] font-mono font-bold ${
                        item.isCurrent
                          ? 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-400 border border-emerald-500/30'
                          : 'bg-muted text-muted-foreground border border-border/60'
                      }`}
                    >
                      v{item.version}
                      {item.isCurrent && ' · 当前版本'}
                    </span>
                    <span className="inline-flex items-center gap-1 text-[11px] text-muted-foreground">
                      <Clock className="h-3 w-3" />
                      <span>
                        {formatTime(item.valid_from)}
                        {' → '}
                        {item.valid_to ? formatTime(item.valid_to) : '至今 (生效中)'}
                      </span>
                    </span>
                  </div>

                  <div className="flex items-center gap-2">
                    {renderVerificationBadge(item.verified_by)}
                    <div className="flex items-center gap-1 text-[11px] text-muted-foreground">
                      <span>置信度</span>
                      <ConfidenceRing value={item.confidence} size={20} strokeWidth={2.5} />
                    </div>
                  </div>
                </div>

                <div className="p-4 space-y-3">
                  {/* 标题变动提示 */}
                  {titleChanged && item.previous && (
                    <div className="rounded-lg bg-amber-500/10 border border-amber-500/20 p-2.5 text-xs text-amber-900 dark:text-amber-200 flex flex-wrap items-center gap-1.5">
                      <span className="font-semibold shrink-0 text-[11px]">标题变更：</span>
                      <span className="line-through text-muted-foreground font-mono text-[11px]">
                        {item.previous.title}
                      </span>
                      <ArrowRight className="h-3 w-3 shrink-0 text-amber-700 dark:text-amber-400" />
                      <span className="font-medium font-mono text-[11px] text-foreground">
                        {item.title}
                      </span>
                    </div>
                  )}

                  {!titleChanged && (
                    <div className="text-xs">
                      <span className="text-[11px] text-muted-foreground">卡片标题：</span>
                      <span className="ml-1.5 font-medium text-foreground">{item.title}</span>
                    </div>
                  )}

                  {/* 正文对照：上一版 vs 这一版 */}
                  {hasPrev && item.previous ? (
                    <div>
                      <div className="mb-1.5 flex items-center justify-between text-[11px] text-muted-foreground">
                        <span>正文对照（从什么改成了什么）：</span>
                      </div>

                      <div
                        className={
                          layoutMode === 'split'
                            ? 'grid grid-cols-1 md:grid-cols-2 gap-3 items-stretch'
                            : 'space-y-2.5'
                        }
                      >
                        {/* 上一版 */}
                        <div className="rounded-lg border border-rose-500/20 bg-rose-500/5 p-3 flex flex-col justify-between">
                          <div className="flex items-center justify-between pb-2 border-b border-rose-500/15 text-[10px] text-rose-700 dark:text-rose-300 font-medium">
                            <span>上一版 (v{item.previous.version})</span>
                            <span className="font-mono">
                              生效于 {formatTime(item.previous.valid_from)}
                            </span>
                          </div>
                          <pre className="mt-2 max-h-56 overflow-y-auto font-mono text-[11px] text-muted-foreground leading-relaxed whitespace-pre-wrap">
                            <MarkdownView>{item.previous.body}</MarkdownView>
                          </pre>
                        </div>

                        {/* 这一版 */}
                        <div className="rounded-lg border border-emerald-500/25 bg-emerald-500/5 p-3 flex flex-col justify-between">
                          <div className="flex items-center justify-between pb-2 border-b border-emerald-500/15 text-[10px] text-emerald-700 dark:text-emerald-300 font-semibold">
                            <span>
                              这一版 (v{item.version}
                              {item.isCurrent ? ' · 当前' : ''})
                            </span>
                            <span className="font-mono">
                              {item.isCurrent
                                ? `生效中 (${formatTime(item.valid_from)})`
                                : `截止于 ${formatTime(item.valid_to)}`}
                            </span>
                          </div>
                          <pre className="mt-2 max-h-56 overflow-y-auto font-mono text-[11px] text-foreground leading-relaxed whitespace-pre-wrap">
                            <MarkdownView>{item.body}</MarkdownView>
                          </pre>
                        </div>
                      </div>
                    </div>
                  ) : (
                    /* 初始版本（无更早版本可对照） */
                    <div>
                      <div className="flex items-center justify-between pb-1.5 text-[11px] text-muted-foreground">
                        <span>初始版本正文 (v{item.version})：</span>
                        <span className="text-[10px] text-muted-foreground">初始创建</span>
                      </div>
                      <pre className="rounded-lg border border-border/60 bg-muted/20 p-3 font-mono text-[11px] text-foreground leading-relaxed whitespace-pre-wrap max-h-56 overflow-y-auto">
                        <MarkdownView>{item.body}</MarkdownView>
                      </pre>
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
