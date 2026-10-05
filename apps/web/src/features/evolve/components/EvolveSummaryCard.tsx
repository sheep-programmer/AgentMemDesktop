import React, { useEffect, useState } from 'react';
import { motion } from 'motion/react';
import { Trophy, TrendingUp, TrendingDown, Minus, Sparkles, CheckCircle2, Hourglass, ArrowRight } from 'lucide-react';
import { cn } from '@/lib/utils';

interface EvolveSummaryCardProps {
  scoreBefore: number;
  scoreAfter: number;
  summary: string;
  /** 本轮晋升为生效的经验数。 */
  promoted?: number;
  /** 本轮结束时仍停在候选、没有被测验题验证的经验数。 */
  pendingCandidates?: number;
  /** 去记忆页处理候选经验。 */
  onReviewCandidates?: () => void;
}

export function EvolveSummaryCard({
  scoreBefore,
  scoreAfter,
  summary,
  promoted = 0,
  pendingCandidates = 0,
  onReviewCandidates,
}: EvolveSummaryCardProps) {
  const delta = +(scoreAfter - scoreBefore).toFixed(1);
  const [animatedScore, setAnimatedScore] = useState(scoreBefore);

  useEffect(() => {
    const start = scoreBefore;
    const duration = 1200;
    const startTime = performance.now();

    const update = (now: number) => {
      const progress = Math.min((now - startTime) / duration, 1);
      // easeOutCubic
      const factor = 1 - Math.pow(1 - progress, 3);
      const current = +(start + (scoreAfter - start) * factor).toFixed(1);
      setAnimatedScore(current);

      if (progress < 1) {
        requestAnimationFrame(update);
      }
    };

    requestAnimationFrame(update);
  }, [scoreBefore, scoreAfter]);

  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.95 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ duration: 0.5 }}
      className="relative overflow-hidden rounded-2xl border border-accent-insight/50 bg-accent-insight/5 p-6 md:p-8 text-center shadow-lg"
    >
      <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-2xl bg-accent-insight/20 text-accent-insight shadow-xs">
        <Trophy className="h-7 w-7" />
      </div>

      <div className="mt-4 flex items-center justify-center gap-2 text-xs font-semibold uppercase tracking-wider text-accent-insight">
        <Sparkles className="h-3.5 w-3.5" />
        <span>本次闭环进化圆满完成</span>
      </div>

      {/* 专家度跃迁大字 */}
      <div className="mt-3 flex items-baseline justify-center gap-3 font-mono">
        <span className="text-2xl font-bold text-muted-foreground line-through opacity-70">
          {scoreBefore.toFixed(1)}
        </span>
        <span className="text-4xl font-extrabold tracking-tight text-foreground sm:text-5xl">
          {animatedScore.toFixed(1)}
        </span>
        <span
          className={cn(
            'flex items-center text-xl font-bold',
            delta > 0 && 'text-accent-insight',
            delta < 0 && 'text-destructive',
            delta === 0 && 'text-muted-foreground',
          )}
        >
          {delta > 0 && <TrendingUp className="h-5 w-5 mr-1 stroke-[2.5]" />}
          {delta < 0 && <TrendingDown className="h-5 w-5 mr-1 stroke-[2.5]" />}
          {delta === 0 && <Minus className="h-5 w-5 mr-1" />}
          {delta > 0 ? `+${delta}` : `${delta}`}
        </span>
      </div>

      <p className="mx-auto mt-3 max-w-lg text-sm text-muted-foreground leading-relaxed">
        {summary}
      </p>

      {/* 这里原本无条件写着「新经验已立即生效」。没有测验题时候选根本不会转正，
          用户照着这句话去提问、发现没学会，只会以为进化坏了。只说真实发生了的事。 */}
      <div className="mt-5 flex flex-col items-center gap-2">
        {promoted > 0 && (
          <div className="inline-flex items-center gap-2 rounded-full border border-border bg-card px-4 py-1.5 text-xs text-foreground font-medium shadow-2xs">
            <CheckCircle2 className="h-4 w-4 text-accent-insight" />
            <span>{promoted} 条经验已通过验证，已在对话与检索中生效</span>
          </div>
        )}
        {pendingCandidates > 0 && (
          <div className="inline-flex flex-wrap items-center justify-center gap-2 rounded-full border border-amber-500/40 bg-amber-500/10 px-4 py-1.5 text-xs font-medium text-amber-700 dark:text-amber-300 shadow-2xs">
            <Hourglass className="h-4 w-4 shrink-0" />
            <span>{pendingCandidates} 条候选经验待验证，可在记忆页手动启用</span>
            {onReviewCandidates && (
              <button
                type="button"
                onClick={onReviewCandidates}
                className="inline-flex items-center gap-0.5 underline-offset-2 hover:underline cursor-pointer"
              >
                去查看
                <ArrowRight className="h-3 w-3" />
              </button>
            )}
          </div>
        )}
        {promoted === 0 && pendingCandidates === 0 && (
          <div className="inline-flex items-center gap-2 rounded-full border border-border bg-card px-4 py-1.5 text-xs text-muted-foreground font-medium shadow-2xs">
            <Minus className="h-4 w-4" />
            <span>本轮没有新的经验生效</span>
          </div>
        )}
      </div>
    </motion.div>
  );
}
