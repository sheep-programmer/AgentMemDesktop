import { useState } from 'react';
import { FlaskConical, Loader2, TrendingDown, TrendingUp } from 'lucide-react';
import { toast } from 'sonner';

import { Button } from '@/components/ui/button';
import { expertiseService } from '@/lib/api';
import type { InsightAttribution } from '@/lib/api/types';

interface InsightAttributionPanelProps {
  spaceId: string;
  /** 要归因的经验（通常就是「值得复查」的那几条）。 */
  insightIds: string[];
}

/**
 * 留一法归因：把每条经验单独拿掉重跑同一批题，看分数掉多少。
 *
 * 放在「值得复查」旁边是有意的：那份列表给的是相关性（一次差评按注入条数均摊），
 * 而这里做的是对照实验。两者摆在一起，用户才看得出「线索」和「结论」的区别。
 *
 * 按钮上写明代价：每条经验多跑一整轮评测，点下去是要花钱和时间的。
 */
export function InsightAttributionPanel({ spaceId, insightIds }: InsightAttributionPanelProps) {
  const [result, setResult] = useState<InsightAttribution | null>(null);
  const [isRunning, setIsRunning] = useState(false);

  const rounds = insightIds.length + 2; // N 条各一轮 + 基准 + A/A

  const run = async () => {
    setIsRunning(true);
    try {
      const res = await expertiseService.attributeInsights(spaceId, {
        insight_ids: insightIds,
        include_noise_floor: true,
      });
      setResult(res);
      toast.success(`归因完成：跑了 ${rounds} 轮评测`);
    } catch (err: unknown) {
      toast.error((err as Error)?.message || '归因失败，请确认测验集与模型配置');
    } finally {
      setIsRunning(false);
    }
  };

  if (insightIds.length === 0) return null;

  const floor = result?.noise_floor ?? null;

  return (
    <div className="rounded-lg border border-border bg-background/60 p-3 space-y-2.5">
      <div className="flex items-start justify-between gap-3">
        <div className="text-xs">
          <div className="font-medium text-foreground">想知道是不是真的它的问题？</div>
          <p className="mt-0.5 text-[11px] leading-relaxed text-muted-foreground">
            留一法：把每条经验单独拿掉，用同一批题重跑一遍，分数掉多少就是它的贡献。
            这是对照实验，不是上面那种统计线索——代价是要跑 {rounds} 轮评测。
          </p>
        </div>
        <Button
          size="sm"
          variant="outline"
          onClick={run}
          disabled={isRunning}
          className="h-7 shrink-0 gap-1 text-xs"
        >
          {isRunning ? <Loader2 className="h-3 w-3 animate-spin" /> : <FlaskConical className="h-3 w-3" />}
          {isRunning ? `跑第 ${rounds} 轮中…` : '跑一次归因'}
        </Button>
      </div>

      {result && (
        <div className="space-y-2 border-t border-border/50 pt-2.5">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
            <span>
              基准分 <strong className="text-foreground">{result.baseline_score.toFixed(1)}</strong>
            </span>
            <span>·</span>
            <span>{result.items} 道题</span>
            {floor !== null && (
              <>
                <span>·</span>
                <span>
                  噪声底 <strong className="text-foreground">±{floor.toFixed(1)}</strong>
                  （贡献小于它的只是抖动，别当结论）
                </span>
              </>
            )}
          </div>

          {(result.contributions ?? []).map((item) => {
            const meaningful = floor === null || Math.abs(item.contribution) > floor;
            const helps = item.contribution > 0;
            return (
              <div
                key={item.insight_id}
                className="flex items-center justify-between gap-3 rounded-md border border-border/60 bg-card px-2.5 py-2 text-[11px]"
              >
                <span className="truncate text-foreground" title={item.trigger}>
                  {item.trigger || item.insight_id}
                </span>
                <span
                  className={`flex shrink-0 items-center gap-1 font-mono font-semibold ${
                    !meaningful
                      ? 'text-muted-foreground'
                      : helps
                        ? 'text-accent-insight'
                        : 'text-destructive'
                  }`}
                  title={`去掉它之后总分 ${item.score_without.toFixed(1)}`}
                >
                  {helps ? (
                    <TrendingUp className="h-3 w-3" />
                  ) : (
                    <TrendingDown className="h-3 w-3" />
                  )}
                  {item.contribution > 0 ? '+' : ''}
                  {item.contribution.toFixed(1)}
                  {!meaningful && <span className="font-sans font-normal">（在噪声内）</span>}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
