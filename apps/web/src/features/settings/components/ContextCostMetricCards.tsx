import React from 'react';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Zap, ArrowDownRight, Layers } from 'lucide-react';

interface MetricCardsProps {
  promptTokens: number;
  cachedTokens: number;
  cacheWriteTokens: number;
  overallHitRate: number;
  completionTokens: number;
}

function formatTokens(count: number): string {
  if (count >= 1_000_000) {
    return `${(count / 1_000_000).toFixed(2)}M`;
  }
  if (count >= 10_000) {
    return `${(count / 1_000).toFixed(1)}k`;
  }
  return count.toLocaleString();
}

export function ContextCostMetricCards({
  promptTokens,
  cachedTokens,
  cacheWriteTokens,
  overallHitRate,
  completionTokens,
}: MetricCardsProps) {
  const hitRatePercent = (overallHitRate * 100).toFixed(1);

  return (
    <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
      {/* 1. 总输入 Token 卡片 */}
      <Card className="border border-border/80 bg-card/60 backdrop-blur-xs shadow-2xs">
        <CardContent className="p-4 sm:p-5 flex flex-col justify-between h-full space-y-3">
          <div className="flex items-center justify-between">
            <span className="text-xs font-medium uppercase tracking-wider text-muted-foreground flex items-center gap-1.5">
              <Layers className="h-3.5 w-3.5 text-muted-foreground" />
              发送给模型
            </span>
            <span className="text-[11px] font-mono text-muted-foreground tabular-nums">
              输入 token
            </span>
          </div>

          <div>
            <div
              className="text-2xl sm:text-3xl font-bold font-mono tracking-tight text-foreground tabular-nums cursor-default"
              title={`${promptTokens.toLocaleString()} tokens`}
            >
              {formatTokens(promptTokens)}
            </div>
            <p className="text-xs text-muted-foreground mt-1 leading-relaxed">
              含普通输入与前缀缓存（不含输出）
            </p>
          </div>

          <div className="pt-2 border-t border-border/40 text-[11px] font-mono text-muted-foreground flex items-center justify-between">
            <span>缓存写入 (Anthropic)</span>
            <span
              className="tabular-nums font-medium text-foreground cursor-default"
              title={`${cacheWriteTokens.toLocaleString()} tokens`}
            >
              {formatTokens(cacheWriteTokens)}
            </span>
          </div>
        </CardContent>
      </Card>

      {/* 2. 综合缓存命中率 —— 全场主角，视觉权重最高 */}
      <Card className="relative overflow-hidden border-2 border-accent-ai/40 bg-accent-ai/5 shadow-xs">
        {/* 顶部环境微光 */}
        <div className="pointer-events-none absolute -top-12 left-1/2 -translate-x-1/2 h-24 w-48 rounded-full bg-accent-ai/15 blur-2xl" />

        <CardContent className="relative p-4 sm:p-5 flex flex-col justify-between h-full space-y-3">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold uppercase tracking-wider text-accent-ai flex items-center gap-1.5">
              <Zap className="h-3.5 w-3.5 text-accent-ai fill-accent-ai/20" />
              前缀缓存命中率
            </span>
            <Badge
              variant="outline"
              className="border-accent-ai/40 bg-accent-ai/10 text-accent-ai text-[10px] px-1.5 py-0 font-medium"
            >
              核心指标
            </Badge>
          </div>

          <div>
            <div className="flex items-baseline gap-1">
              <span className="text-3xl sm:text-4xl font-extrabold font-mono tracking-tight text-accent-ai tabular-nums">
                {hitRatePercent}
              </span>
              <span className="text-lg font-bold font-mono text-accent-ai">
                %
              </span>
            </div>
            <p className="text-xs text-foreground/85 font-medium mt-1 leading-relaxed">
              <span
                className="font-mono tabular-nums text-accent-ai cursor-default"
                title={`${cachedTokens.toLocaleString()} tokens`}
              >
                {formatTokens(cachedTokens)}
              </span>{' '}
              token 已被服务商复用
            </p>
          </div>

          <div className="pt-2 border-t border-accent-ai/20 text-[11px] text-muted-foreground flex items-center justify-between">
            <span>可复用前缀</span>
            <span className="text-accent-ai font-medium">
              固定规则与部分对话历史
            </span>
          </div>
        </CardContent>
      </Card>

      {/* 3. 预估省下费用比例 卡片 */}
      <Card className="border border-border/80 bg-card/60 backdrop-blur-xs shadow-2xs">
        <CardContent className="p-4 sm:p-5 flex flex-col justify-between h-full space-y-3">
          <div className="flex items-center justify-between">
            <span className="text-xs font-medium uppercase tracking-wider text-muted-foreground flex items-center gap-1.5">
              <ArrowDownRight className="h-3.5 w-3.5 text-accent-insight" />
              模型生成内容
            </span>
            <span className="text-[11px] font-mono text-muted-foreground tabular-nums">
              输出 token
            </span>
          </div>

          <div>
            <div className="flex items-baseline gap-1">
              <span className="text-2xl sm:text-3xl font-bold font-mono tracking-tight text-accent-insight tabular-nums">
                {formatTokens(completionTokens)}
              </span>
            </div>
            <p className="text-xs text-muted-foreground mt-1 leading-relaxed">
              由服务商返回的回答用量
            </p>
          </div>

          <div className="pt-2 border-t border-border/40 text-[11px] font-mono text-muted-foreground flex items-center justify-between">
            <span>实际费用</span>
            <span className="text-muted-foreground">以服务商账单为准</span>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
