import React from 'react';
import type { EvolvePendingSummary } from '@/lib/api/types.temp';
import { isMockMode } from '@/lib/api/client';
import { Button } from '@/components/ui/button';
import {
  Sparkles,
  MessageSquare,
  Edit3,
  ArrowRight,
  Loader2,
  Square,
} from 'lucide-react';

interface EvolveHeroCardProps {
  pending: EvolvePendingSummary;
  isEvolving: boolean;
  isChecking?: boolean;
  onStartEvolve: () => void;
  onStopEvolve?: () => void;
}

export function EvolveHeroCard({
  pending,
  isEvolving,
  isChecking = false,
  onStartEvolve,
  onStopEvolve,
}: EvolveHeroCardProps) {
  const feedbackCount = pending.feedback_count ?? 0;
  const correctionCount = pending.correction_count ?? 0;
  // feedback_count 是全部待学习的反馈（含纠错），correction_count 是其中的纠错
  const hasData = feedbackCount > 0;
  const canEvolve = hasData || isMockMode();

  return (
    <div className="relative overflow-hidden rounded-2xl border border-primary/25 bg-gradient-to-br from-primary/8 via-card to-card p-6 md:p-8 shadow-xs select-none">
      {/* 柔和环境微光 */}
      <div className="pointer-events-none absolute -bottom-10 -left-10 h-44 w-44 rounded-full bg-accent-ai/10 blur-3xl" />
      <div className="pointer-events-none absolute -top-10 -right-10 h-44 w-44 rounded-full bg-accent-insight/10 blur-3xl" />

      <div className="relative z-10 flex flex-col lg:flex-row lg:items-center justify-between gap-6">
        <div className="space-y-4 max-w-2xl">
          <div className="inline-flex items-center gap-1.5 rounded-full border border-primary/30 bg-primary/10 px-3 py-1 text-xs font-semibold text-primary">
            <Sparkles className="h-3.5 w-3.5" />
            <span>自我进化</span>
          </div>

          <div>
            <h2 className="text-xl font-bold tracking-tight text-foreground sm:text-2xl">
              待学习素材与闭环进化
            </h2>
            <p className="mt-1.5 text-xs sm:text-sm text-muted-foreground leading-relaxed">
              系统会从你的纠错和差评里总结出候选经验，先用测验题验证确实让回答变好，再正式启用。
            </p>
          </div>

          {/* 醒目的统计块 */}
          <div className="grid grid-cols-2 gap-3 pt-1 sm:max-w-md">
            <div className="flex items-center gap-3.5 rounded-xl border border-border/70 bg-background/70 px-4 py-3 shadow-2xs backdrop-blur-xs">
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-accent-ai/25 bg-accent-ai/10 text-accent-ai">
                <MessageSquare className="h-5 w-5" />
              </div>
              <div className="min-w-0">
                <div className="font-mono text-2xl font-bold tabular-nums text-foreground leading-none">
                  {feedbackCount}
                </div>
                <div className="mt-1 text-[11px] font-medium tracking-wide text-muted-foreground">
                  条待学习的反馈
                </div>
              </div>
            </div>

            <div className="flex items-center gap-3.5 rounded-xl border border-border/70 bg-background/70 px-4 py-3 shadow-2xs backdrop-blur-xs">
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-accent-insight/25 bg-accent-insight/10 text-accent-insight">
                <Edit3 className="h-5 w-5" />
              </div>
              <div className="min-w-0">
                <div className="font-mono text-2xl font-bold tabular-nums text-foreground leading-none">
                  {correctionCount}
                </div>
                <div className="mt-1 text-[11px] font-medium tracking-wide text-muted-foreground">
                  条其中是纠错
                </div>
              </div>
            </div>
          </div>
          {/* 素材从哪来要说出来：只摆两个数字，新用户不知道该去哪儿产生反馈 */}
          <p className="text-[11px] leading-relaxed text-muted-foreground sm:max-w-md">
            素材来自对话页：在回答下方点「满意」「不满意」或「纠错」，这些反馈就会出现在这里。纠错最有用——它直接告诉系统正确答案是什么。
          </p>
        </div>

        <div className="shrink-0 flex flex-col items-start lg:items-end justify-center">
          <Button
            size="lg"
            onClick={onStartEvolve}
            disabled={isEvolving || isChecking || !canEvolve}
            title={
              !canEvolve ? '至少要有 1 条反馈或纠错才能开始进化' : undefined
            }
            className="h-11 px-6 gap-2 text-sm font-semibold bg-primary text-primary-foreground shadow-sm hover:bg-primary/90 transition-all duration-200 hover:scale-[1.01] active:scale-[0.99] cursor-pointer disabled:cursor-not-allowed"
          >
            {isEvolving || isChecking ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" />
                <span>{isChecking ? '检查测验题...' : '进化进行中...'}</span>
              </>
            ) : (
              <>
                <Sparkles className="h-4 w-4" />
                <span>开始一次进化</span>
                <ArrowRight className="h-4 w-4" />
              </>
            )}
          </Button>
          {isEvolving && onStopEvolve && (
            <Button
              variant="outline"
              size="sm"
              onClick={onStopEvolve}
              className="mt-3 gap-1.5 text-destructive"
            >
              <Square className="h-3.5 w-3.5" />
              停止进化
            </Button>
          )}
          <div className="mt-2 text-[11px] text-muted-foreground tracking-wide">
            {isEvolving
              ? '停止会中止请求，已生成的候选经验会保留'
              : hasData
                ? '素材已就绪，可以开始进化'
                : '还没有素材：先去对话页给几条回答反馈'}
          </div>
        </div>
      </div>
    </div>
  );
}
