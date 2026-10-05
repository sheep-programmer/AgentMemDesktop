import React from 'react';
import type { KnowledgeCard } from '@/lib/api/types.temp';
import { markdownToPlainText } from '@/lib/markdown';
import { ConfidenceRing } from '@/components/shared/ConfidenceRing';
import { BadgeCheck, ArrowUpRight, History } from 'lucide-react';
import { cn } from '@/lib/utils';

type KnowledgeCardKind = KnowledgeCard['kind'];

interface KnowledgeCardItemProps {
  card: KnowledgeCard;
  onClick: (card: KnowledgeCard, initialTab?: 'edit' | 'history') => void;
}

export function KnowledgeCardItem({ card, onClick }: KnowledgeCardItemProps) {
  const getKindBorderColor = (kind: KnowledgeCardKind) => {
    switch (kind) {
      case 'concept':
        return 'border-l-primary';
      case 'fact':
        return 'border-l-muted-foreground/40';
      case 'procedure':
        return 'border-l-accent-ai';
      case 'pitfall':
        return 'border-l-accent-warn';
      case 'tool':
        return 'border-l-accent-insight';
      default:
        return 'border-l-primary';
    }
  };

  const getKindLabel = (kind: KnowledgeCardKind) => {
    switch (kind) {
      case 'concept':
        return '概念';
      case 'fact':
        return '事实';
      case 'procedure':
        return '规程';
      case 'pitfall':
        return '陷阱';
      case 'tool':
        return '工具';
    }
  };

  return (
    <div
      onClick={() => onClick(card, 'edit')}
      className={cn(
        'group flex h-full flex-col justify-between rounded-xl border border-border/70 bg-card p-4.5 text-xs shadow-2xs transition-all duration-200 ease-out hover:-translate-y-0.5 hover:border-primary/40 hover:shadow-xs border-l-4 cursor-pointer select-none',
        getKindBorderColor(card.kind),
      )}
    >
      <div>
        {/* 头部：类别与置信度 + 已核验徽章精准对齐 */}
        <div className="flex items-center justify-between gap-2">
          <span className="font-mono text-[11px] font-semibold text-muted-foreground uppercase tracking-wide">
            {getKindLabel(card.kind)}
          </span>
          <div className="flex items-center gap-2 shrink-0">
            {card.verified_by && (
              <span
                className="inline-flex items-center gap-1 rounded-md bg-accent-insight/10 px-2 py-0.5 text-[10px] font-medium text-accent-insight border border-accent-insight/25 tracking-wide leading-none"
                title="已人工核验"
              >
                <BadgeCheck className="h-3.5 w-3.5" />
                已核验
              </span>
            )}
            <ConfidenceRing value={card.confidence} size={22} strokeWidth={2.5} />
          </div>
        </div>

        {/* 标题 */}
        <h3 className="mt-3 text-sm font-semibold text-foreground group-hover:text-primary transition-colors line-clamp-1 leading-snug">
          {card.title}
        </h3>

        {/* 正文摘要 */}
        <p className="mt-2 line-clamp-3 text-muted-foreground leading-relaxed text-xs">
          {markdownToPlainText(card.body)}
        </p>
      </div>

      {/* 底部别名标签与抽屉展开引导 */}
      <div className="mt-4 flex items-center justify-between border-t border-border/40 pt-3 text-[11px] text-muted-foreground">
        <div className="flex flex-wrap items-center gap-1.5 min-w-0">
          {card.aliases?.slice(0, 3).map((alias) => (
            // inline-block 而非 inline-flex：truncate 的 text-overflow 不作用于 flex 项，
            // 写成 inline-flex 时长别名是被硬切成半个字的，没有省略号
            <span
              key={alias}
              title={alias}
              className="inline-block rounded-md border border-border/60 bg-muted/40 px-2 py-0.5 text-[10px] font-mono text-muted-foreground hover:bg-muted transition-colors truncate max-w-[140px]"
            >
              #{alias}
            </span>
          ))}
        </div>
        <div className="flex items-center gap-2.5 text-[10px] font-medium opacity-0 group-hover:opacity-100 transition-opacity shrink-0 ml-2">
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              onClick(card, 'history');
            }}
            className="inline-flex items-center text-muted-foreground hover:text-foreground transition-colors cursor-pointer"
            title="查看版本留档历史"
          >
            <History className="h-3 w-3 mr-0.5" />
            <span>历史</span>
          </button>
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              onClick(card, 'edit');
            }}
            className="inline-flex items-center text-primary hover:text-primary/80 transition-colors cursor-pointer"
            title="查看详情与编辑"
          >
            <span>详情</span>
            <ArrowUpRight className="h-3.5 w-3.5 ml-0.5" />
          </button>
        </div>
      </div>
    </div>
  );
}
