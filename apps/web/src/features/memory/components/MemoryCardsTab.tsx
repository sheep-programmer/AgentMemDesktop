import React from 'react';
import type { KnowledgeCard } from '@/lib/api/types.temp';
import { KnowledgeCardItem } from './KnowledgeCardItem';
import { FourStateView } from '@/components/shared/FourStateView';
import { Search, Loader2, Sparkles } from 'lucide-react';
import { Button } from '@/components/ui/button';

interface MemoryCardsTabProps {
  cards: KnowledgeCard[];
  pageStatus: 'loading' | 'empty' | 'error' | 'ready';
  searchQuery: string;
  onSearchChange: (q: string) => void;
  kindFilter: string;
  onKindFilterChange: (k: string) => void;
  onCardClick: (card: KnowledgeCard, initialTab?: 'edit' | 'history') => void;
  onRetry: () => void;
  nextCursor?: string | null;
  isLoadingMore?: boolean;
  onLoadMore?: () => void;
  onExtract?: () => void;
  isExtracting?: boolean;
  extractDetail?: string | null;
  canExtract?: boolean;
}

const KINDS = ['all', 'concept', 'procedure', 'pitfall', 'tool', 'fact'] as const;

/** 卡片类别的中文说法：界面上不出现 concept / procedure 这类内部取值。 */
const KIND_LABELS: Record<(typeof KINDS)[number], string> = {
  all: '全部',
  concept: '概念',
  procedure: '规程',
  pitfall: '陷阱',
  tool: '工具',
  fact: '事实',
};

export function MemoryCardsTab({
  cards,
  pageStatus,
  searchQuery,
  onSearchChange,
  kindFilter,
  onKindFilterChange,
  onCardClick,
  onRetry,
  nextCursor,
  isLoadingMore,
  onLoadMore,
  onExtract,
  isExtracting,
  extractDetail,
  canExtract = true,
}: MemoryCardsTabProps) {
  const filteredCards = cards.filter((c) =>
    (kindFilter === 'all' || c.kind === kindFilter) &&
    (c.title.toLowerCase().includes(searchQuery.toLowerCase()) ||
      c.body.toLowerCase().includes(searchQuery.toLowerCase())),
  );

  return (
    <FourStateView
      status={cards.length === 0 && pageStatus === 'ready' ? 'empty' : pageStatus}
      emptyTitle="尚未生成知识卡片"
      emptyDescription={
        canExtract
          ? '卡片由模型从已就绪的文档里抽取：概念、规程、陷阱、事实与工具。也可以直接在资料库里导入更多文档。'
          : '抽取卡片需要「经验蒸馏」角色绑定一个可用模型，请先到「系统设置 → 模型」里配置。'
      }
      emptyActionLabel={canExtract ? (isExtracting ? '正在抽取…' : '从文档抽取卡片') : undefined}
      onEmptyAction={canExtract ? onExtract : undefined}
      error="无法加载知识卡片，请检查后端状态。"
      onRetry={onRetry}
    >
      <div className="space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="relative flex-1 max-w-sm">
            <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
            <input
              type="text"
              placeholder="搜索知识卡片..."
              value={searchQuery}
              onChange={(e) => onSearchChange(e.target.value)}
              className="w-full rounded-lg border border-border/80 bg-card/60 py-1.5 pl-9 pr-3 text-xs text-foreground placeholder:text-muted-foreground outline-none focus:border-primary/50 focus:ring-1 focus:ring-primary/20 transition-all"
            />
          </div>
          <div className="flex items-center gap-2">
            {onExtract && (
              <Button
                size="sm"
                variant="outline"
                onClick={onExtract}
                disabled={isExtracting || !canExtract}
                title={
                  canExtract
                    ? '从已就绪的文档里抽取概念、规程、陷阱等知识卡片'
                    : '需要在「系统设置 → 模型」里给「经验蒸馏」角色绑定一个可用模型'
                }
                className="h-8 gap-1.5 text-xs"
              >
                {isExtracting ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Sparkles className="h-3.5 w-3.5" />
                )}
                {isExtracting ? '正在抽取…' : '抽取知识卡片'}
              </Button>
            )}
            {extractDetail && (
              <span className="font-mono text-[11px] text-muted-foreground">{extractDetail}</span>
            )}
          </div>
          <div className="flex items-center gap-1 text-xs">
            {KINDS.map((k) => (
              <button
                key={k}
                type="button"
                onClick={() => onKindFilterChange(k)}
                className={`rounded-md px-2.5 py-1 text-xs transition-colors cursor-pointer ${
                  kindFilter === k
                    ? 'bg-primary text-primary-foreground font-medium'
                    : 'bg-muted/40 text-muted-foreground hover:bg-muted'
                }`}
              >
                {KIND_LABELS[k]}
              </button>
            ))}
          </div>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4.5 pt-1 items-stretch">
          {filteredCards.map((card) => (
            <KnowledgeCardItem
              key={card.id}
              card={card}
              onClick={(c, tab) => onCardClick(c, tab)}
            />
          ))}
        </div>

        {/* 分页契约：加载更多 */}
        {nextCursor && (
          <div className="flex justify-center pt-4 pb-2">
            <Button
              variant="outline"
              size="sm"
              onClick={onLoadMore}
              disabled={isLoadingMore}
              className="gap-2 text-xs"
            >
              {isLoadingMore ? (
                <>
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  <span>正在加载更多卡片...</span>
                </>
              ) : (
                <span>加载更多卡片</span>
              )}
            </Button>
          </div>
        )}
      </div>
    </FourStateView>
  );
}
