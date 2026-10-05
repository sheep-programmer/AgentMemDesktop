import React, { useState } from 'react';
import { motion, AnimatePresence } from 'motion/react';
import {
  CheckCircle2,
  Loader2,
  ChevronDown,
  ChevronUp,
} from 'lucide-react';
import { cn } from '@/lib/utils';

export interface PipelineStageState {
  id: string;
  name: string;
  status: 'pending' | 'running' | 'completed';
  summary: string;
  detail: string;
  metrics?: Record<string, string | number>;
}

/** 后端 `stage` 事件里 detail 拍平后的字段，按阶段名归集。 */
export type EvolveStageData = Record<string, Record<string, unknown>>;

interface EvolvePipelineStagesProps {
  currentStageIndex: number;
  /** 各阶段的真实数据，由 EvolvePage 从 SSE 的 stage 事件累积而来 */
  stageData?: EvolveStageData;
}

function num(source: Record<string, unknown> | undefined, key: string): number | null {
  const value = source?.[key];
  return typeof value === 'number' ? value : null;
}

export function EvolvePipelineStages({
  currentStageIndex,
  stageData = {},
}: EvolvePipelineStagesProps) {
  const [expandedStage, setExpandedStage] = useState<string | null>(null);
  // 展开的卡片跟着正在跑的阶段走。此前固定写死 'stage-1'，一轮进化从头到尾
  // 摊开的都是第一阶段，看起来就像「卡在阶段 1 不动」——其实后面几步早跑过去了。
  const autoExpanded = expandedStage ?? `stage-${Math.min(Math.max(currentStageIndex, 0), 3) + 1}`;

  // 所有数字一律来自后端 stage 事件；没收到就说「进行中」，绝不编一个看着像真的值。
  // 这里原先四个阶段的 summary 与 metrics 全是写死的常量（「从 17 条反馈与 5 条人工
  // 纠错中蒸馏出 7 条」「基线 62.4 → 71.8」…），而实测那一轮真实值是 2 条反馈、
  // 50.25 → 81.38。对一个卖点是「可验证的进化」的产品，展示编造的测量值是致命的。
  const distill = stageData.distill;
  const consolidate = stageData.consolidate;
  const evaluate = stageData.evaluate;
  const promote = stageData.promote;

  const produced = num(distill, 'produced');
  const skipped = num(distill, 'skipped');
  const merged = num(consolidate, 'merged');
  const duplicates = num(consolidate, 'duplicates');
  const conflicts = num(consolidate, 'conflicts');
  const baseline = num(evaluate, 'baseline');
  const withInsights = num(evaluate, 'with_insights');
  const evalDelta = num(evaluate, 'delta');
  const runningScore = num(evaluate, 'score');
  const evalSkipped = evaluate?.skipped === true;
  const promoted = num(promote, 'promoted');
  const demoted = num(promote, 'demoted');
  // 两轮评测有效测得题数不足（多半是限流/额度耗尽）时后端会跳过判决。
  // 这时 promoted/demoted 都是 0，若照常渲染成「0 条晋升、0 条归档」，
  // 用户会以为这批经验被评过且毫无作用——实际是根本没评成。
  const promoteSkipped = promote?.skipped === true;
  const promoteSkipReason = typeof promote?.reason === 'string' ? promote.reason : null;

  const pending = (index: number, text: string) =>
    currentStageIndex < index ? '等待上一阶段完成' : currentStageIndex === index ? text : '本阶段无数据上报';

  const stages: PipelineStageState[] = [
    {
      id: 'stage-1',
      name: '阶段 1: 总结经验',
      status: currentStageIndex > 0 ? 'completed' : currentStageIndex === 0 ? 'running' : 'pending',
      summary:
        produced !== null
          ? `蒸馏出 ${produced} 条候选经验${skipped ? `，跳过 ${skipped} 条`: ''}`
          : pending(0, '正在从反馈与纠错中蒸馏候选经验...'),
      detail: '提取触发场景 [Trigger] 与正向行动对策 [Guidance]，剔除偶发噪声，生成结构化候选经验草稿。',
      metrics:
        produced !== null
          ? { 候选生成: `${produced} 条`, 跳过: `${skipped ?? 0} 条` }
          : undefined,
    },
    {
      id: 'stage-2',
      name: '阶段 2: 合并去重',
      status: currentStageIndex > 1 ? 'completed' : currentStageIndex === 1 ? 'running' : 'pending',
      summary:
        merged !== null
          ? `合并 ${merged} 条，归档重复 ${duplicates ?? 0} 条，冲突 ${conflicts ?? 0} 组`
          : pending(1, '正在做语义去重与矛盾检测...'),
      detail: '在当前领域经验图谱中进行语义去重与矛盾性检测，完成两两一致性校验。',
      metrics:
        merged !== null
          ? { 合并: `${merged} 条`, 重复归档: `${duplicates ?? 0} 条`, 冲突: `${conflicts ?? 0} 组` }
          : undefined,
    },
    {
      id: 'stage-3',
      name: '阶段 3: 测验验证',
      status: currentStageIndex > 2 ? 'completed' : currentStageIndex === 2 ? 'running' : 'pending',
      summary: evalSkipped
        ? `已跳过：${String(evaluate?.reason ?? '无测验集或无候选经验')}`
        : baseline !== null && withInsights !== null
        ? `基线 ${baseline} → 注入经验 ${withInsights}（${(evalDelta ?? 0) >= 0 ? '+' : ''}${evalDelta ?? 0}）`
        : runningScore !== null
        ? `基线已测得 ${runningScore}，正在跑注入经验的对照组...`
        : pending(2, '正在用测验题对比启用前后的回答...'),
      detail: '用这个空间的测验题对比启用前后的回答，确认候选经验确实让回答变好。',
      metrics:
        baseline !== null && withInsights !== null
          ? {
              基准得分: baseline,
              经验加持: withInsights,
              提升幅度: `${(evalDelta ?? 0) >= 0 ? '+' : ''}${evalDelta ?? 0}`,
            }
          : undefined,
    },
    {
      id: 'stage-4',
      name: '阶段 4: 正式启用',
      status: currentStageIndex > 3 ? 'completed' : currentStageIndex === 3 ? 'running' : 'pending',
      summary: promoteSkipped
        ? `本轮未判决：${promoteSkipReason ?? '有效测得题数不足'}。候选经验保持原状`
        : promoted !== null
        ? `${promoted} 条晋升为 active，${demoted ?? 0} 条未达标归档`
        : pending(3, '正在按评测结论决定晋升与淘汰...'),
      detail: '把验证有效的经验正式启用，之后遇到相似问题会自动参考。',
      metrics:
        promoted !== null && !promoteSkipped
          ? { 正式晋升: `${promoted} 条`, 淘汰归档: `${demoted ?? 0} 条` }
          : undefined,
    },
  ];

  return (
    <div className="relative py-4 select-none">
      {/* 垂直连接线 */}
      <div className="absolute left-6 top-8 bottom-8 w-0.5 bg-border z-0" />

      <div className="space-y-4 relative z-10">
        {stages.map((stage, idx) => {
          const isCurrent = currentStageIndex === idx;
          const isCompleted = currentStageIndex > idx;
          const isPending = currentStageIndex < idx;
          const isExpanded = autoExpanded === stage.id;

          return (
            <motion.div
              key={stage.id}
              initial={{ opacity: 0, y: 15 }}
              animate={{ opacity: isPending ? 0.45 : 1, y: 0 }}
              transition={{ duration: 0.35, delay: idx * 0.1 }}
              className={cn(
                'ml-12 rounded-xl border p-4 text-xs transition-all',
                isCurrent
                  ? 'border-primary bg-primary/5 ring-2 ring-primary/20 shadow-md dark:shadow-[0_0_20px_color-mix(in_oklab,var(--primary)_18%,transparent)]'
                  : isCompleted
                  ? 'border-accent-insight/40 bg-card shadow-2xs dark:shadow-[0_0_15px_color-mix(in_oklab,var(--accent-insight)_10%,transparent)]'
                  : 'border-border bg-muted/20',
              )}
            >
              {/* 阶段圆圈图标 */}
              <div
                className={cn(
                  'absolute -left-12 top-4 flex h-8 w-8 items-center justify-center rounded-full border shadow-xs',
                  isCurrent
                    ? 'border-primary bg-primary text-primary-foreground animate-pulse'
                    : isCompleted
                    ? 'border-accent-insight bg-accent-insight text-background'
                    : 'border-border bg-card text-muted-foreground',
                )}
              >
                {isCompleted ? (
                  <CheckCircle2 className="h-4 w-4" />
                ) : isCurrent ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <span className="font-mono text-xs">{idx + 1}</span>
                )}
              </div>

              {/* 头部标题与收放切换 */}
              <div
                className="flex cursor-pointer items-center justify-between"
                onClick={() => setExpandedStage(isExpanded ? null : stage.id)}
              >
                <div className="flex items-center gap-2">
                  <span className="font-semibold text-foreground text-sm">{stage.name}</span>
                  {isCurrent && (
                    <span className="rounded-full bg-primary/20 px-2 py-0.5 text-[10px] font-medium text-primary">
                      执行中
                    </span>
                  )}
                  {isCompleted && (
                    <span className="rounded-full bg-accent-insight/20 px-2 py-0.5 text-[10px] font-medium text-accent-insight">
                      已完成
                    </span>
                  )}
                </div>
                {isExpanded ? <ChevronUp className="h-4 w-4 text-muted-foreground" /> : <ChevronDown className="h-4 w-4 text-muted-foreground" />}
              </div>

              {/* 摘要简述 */}
              <p className="mt-1 font-medium text-foreground/90">{stage.summary}</p>

              {/* 展开的详情与指标 */}
              <AnimatePresence>
                {isExpanded && (
                  <motion.div
                    initial={{ opacity: 0, height: 0 }}
                    animate={{ opacity: 1, height: 'auto' }}
                    exit={{ opacity: 0, height: 0 }}
                    className="mt-3 border-t border-border/50 pt-2.5 space-y-2"
                  >
                    <p className="text-muted-foreground leading-relaxed">{stage.detail}</p>
                    {stage.metrics && (
                      <div className="flex flex-wrap gap-3 pt-1">
                        {Object.entries(stage.metrics).map(([key, val]) => (
                          <div key={key} className="rounded-md border border-border/80 bg-background/80 px-2.5 py-1 text-[11px]">
                            <span className="text-muted-foreground">{key}: </span>
                            <span className="font-mono font-semibold text-foreground">{val}</span>
                          </div>
                        ))}
                      </div>
                    )}
                  </motion.div>
                )}
              </AnimatePresence>
            </motion.div>
          );
        })}
      </div>
    </div>
  );
}
