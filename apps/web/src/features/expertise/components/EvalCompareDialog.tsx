import React, { useState, useCallback, useEffect } from 'react';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Switch } from '@/components/ui/switch';
import {
  GitCompare,
  Plus,
  Trash2,
  Play,
  Loader2,
  ChevronDown,
  ChevronUp,
  AlertTriangle,
  AlertCircle,
  Sliders,
  CheckCircle2,
  Sparkles,
  Square,
} from 'lucide-react';
import { useAsyncTask } from '@/hooks/useAsyncTask';
import {
  expertiseService,
  type EvalCompareArmData,
  type EvalCompareDoneData,
  type EvalCompareErrorData,
  type EvalCompareStageData,
} from '@/lib/api/services/expertise';
import type {
  EvalCompareRequest,
  RetrievalKnobOverrides,
} from '@/lib/api/types';
import { RetrievalMetricsView } from './RetrievalMetricsView';
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import { toast } from 'sonner';
import { cn } from '@/lib/utils';

export interface EvalCompareDialogProps {
  isOpen: boolean;
  onClose: () => void;
  spaceId: string;
  evalItemCount: number;
  onComparisonComplete?: () => void;
}

interface ArmConfig {
  id: string;
  label: string;
  enabledKnobs: {
    diversity?: boolean;
    top_n_rerank?: boolean;
    top_k_vector?: boolean;
    top_k_fts?: boolean;
    max_insights?: boolean;
    hyde?: boolean;
    mmr_lambda?: boolean;
    dedup_threshold?: boolean;
  };
  overrides: {
    diversity?: boolean;
    top_n_rerank?: number;
    top_k_vector?: number;
    top_k_fts?: number;
    max_insights?: number;
    hyde?: boolean;
    mmr_lambda?: number;
    dedup_threshold?: number;
  };
  showAdvanced?: boolean;
}

const initialArms: ArmConfig[] = [
  {
    id: 'arm-baseline',
    label: '基准',
    enabledKnobs: {},
    overrides: {},
    showAdvanced: false,
  },
  {
    id: 'arm-no-diversity',
    label: '关掉多样性',
    enabledKnobs: {
      diversity: true,
    },
    overrides: {
      diversity: false,
    },
    showAdvanced: false,
  },
];

interface BipolarDeltaBarProps {
  label: string;
  value: number | null | undefined;
  maxVal?: number;
  digits?: number;
  tooltipTitle?: string;
  tooltipDesc?: string;
  /** 这个差值是否显著。只有显著的才上涨跌色；
   *  此前所有非零差值一律红绿着色，t≈0.6 的 +1.3 也被画成「提升」。
   *  undefined 表示后端没给检验结果（旧数据），同样不上色。 */
  significant?: boolean;
}

function BipolarDeltaBar({
  label,
  value,
  maxVal = 1,
  digits = 3,
  tooltipTitle,
  tooltipDesc,
  significant,
}: BipolarDeltaBarProps) {
  const isUnmeasured = value === null || value === undefined;
  const num = value ?? 0;
  const isZero = Math.abs(num) < 0.0001;
  const isPos = num > 0 && significant === true;
  const isNeg = num < 0 && significant === true;
  const isNoise = !isUnmeasured && !isZero && significant !== true;

  // Normalized width 0% to 50% from center
  const ratio = Math.min(
    50,
    Math.max(1.5, (Math.abs(num) / (maxVal || 1)) * 50),
  );

  const displayStr = isUnmeasured
    ? '未测到'
    : isZero
      ? '±0.000'
      : isPos
        ? `+${num.toFixed(digits)}`
        : num.toFixed(digits);

  return (
    <Tooltip>
      <TooltipTrigger className="w-full text-left cursor-help">
        <div className="flex items-center gap-2 text-xs py-1 hover:bg-muted/40 rounded px-1 transition-colors">
          <span className="w-18 shrink-0 font-medium text-[11px] text-muted-foreground truncate">
            {label}
          </span>

          {/* 0 为中心的横向条形轨 */}
          <div className="flex-1 h-3 rounded bg-muted/60 relative overflow-hidden flex items-center">
            {/* 中心 0 刻度线 */}
            <div className="absolute left-1/2 top-0 bottom-0 w-[1.5px] bg-border/90 z-10" />

            {!isUnmeasured && !isZero && (
              <div
                className={cn(
                  'absolute top-0.5 bottom-0.5 rounded-xs transition-all duration-300',
                  isNoise
                    ? 'bg-muted-foreground/35'
                    : isPos
                      ? 'left-1/2 bg-emerald-500/85 dark:bg-emerald-400/85'
                      : 'bg-rose-500/85 dark:bg-rose-400/85',
                )}
                style={
                  num > 0
                    ? { left: '50%', width: `${ratio}%` }
                    : { right: '50%', width: `${ratio}%` }
                }
              />
            )}

            {!isUnmeasured && isZero && (
              <div className="absolute left-1/2 -translate-x-1/2 h-2 w-1 rounded-full bg-muted-foreground/60" />
            )}
          </div>

          {/* 右侧有符号数值展示（保持有符号三位小数） */}
          <span
            className={cn(
              'w-16 shrink-0 font-mono text-[11px] text-right font-semibold tabular-nums',
              isPos
                ? 'text-emerald-700 dark:text-emerald-400'
                : isNeg
                  ? 'text-destructive'
                  : 'text-muted-foreground',
            )}
          >
            {displayStr}
          </span>
          {isNoise && significant === false && (
            <span className="shrink-0 text-[10px] text-muted-foreground">
              噪声内
            </span>
          )}
        </div>
      </TooltipTrigger>
      <TooltipContent side="top" className="text-xs max-w-xs space-y-1 p-2.5">
        <div className="font-semibold text-foreground flex items-center justify-between gap-2">
          <span>{tooltipTitle || `${label} 差值 (Δ)`}</span>
          <span className="font-mono">
            {isUnmeasured
              ? '未测到 (null)'
              : isPos
                ? `+${num.toFixed(4)}`
                : num.toFixed(4)}
          </span>
        </div>
        {tooltipDesc && (
          <div className="text-[11px] text-muted-foreground leading-relaxed">
            {tooltipDesc}
          </div>
        )}
        {isNoise && (
          <div className="text-[11px] text-muted-foreground leading-relaxed">
            逐题配对检验不显著：这个差值在评测随机波动范围内，不能当作结论。
          </div>
        )}
      </TooltipContent>
    </Tooltip>
  );
}

function ItemDeltaMiniBar({
  index,
  scoreDelta,
  maxAbsScore = 5,
}: {
  index: number;
  scoreDelta: number;
  maxAbsScore?: number;
}) {
  // 单题差值一律不上涨跌色：同配置的 A/A 里单题差值就有 ±15 分的随机波动，
  // 逐题红绿会让人对着噪声下结论
  const isPos = false;
  const isNeg = false;
  const isZero = Math.abs(scoreDelta) < 0.0001;
  const widthPct = Math.min(
    50,
    Math.max(2, (Math.abs(scoreDelta) / (maxAbsScore || 1)) * 50),
  );

  return (
    <div className="p-2 rounded-lg border border-border/60 bg-muted/20 text-xs space-y-1">
      <div className="flex items-center justify-between text-[10.5px] font-mono">
        <span className="text-muted-foreground">题 #{index + 1}</span>
        <span
          className={cn(
            'font-semibold tabular-nums',
            isPos
              ? 'text-emerald-700 dark:text-emerald-400'
              : isNeg
                ? 'text-destructive'
                : 'text-muted-foreground',
          )}
        >
          {isPos ? `+${scoreDelta.toFixed(1)}` : scoreDelta.toFixed(1)}
        </span>
      </div>

      {/* Mini zero-centered bar */}
      <div className="h-1.5 rounded-full bg-muted/60 relative overflow-hidden flex items-center">
        <div className="absolute left-1/2 top-0 bottom-0 w-[1px] bg-border/80 z-10" />
        {!isZero && (
          <div
            className={cn(
              'absolute top-0 bottom-0 rounded-full',
              'bg-muted-foreground/40',
            )}
            style={
              scoreDelta > 0
                ? { left: '50%', width: `${widthPct}%` }
                : { right: '50%', width: `${widthPct}%` }
            }
          />
        )}
      </div>
    </div>
  );
}

export function EvalCompareDialog({
  isOpen,
  onClose,
  spaceId,
  evalItemCount,
  onComparisonComplete,
}: EvalCompareDialogProps) {
  const {
    start: startTask,
    cancel: cancelTask,
    running: taskRunning,
  } = useAsyncTask(isOpen ? spaceId : null);
  const [arms, setArms] = useState<ArmConfig[]>(initialArms);
  const [isRunning, setIsRunning] = useState(false);
  const [currentStage, setCurrentStage] = useState<EvalCompareStageData | null>(
    null,
  );
  const [armOutcomes, setArmOutcomes] = useState<
    Record<string, EvalCompareArmData>
  >({});
  const [comparisonResult, setComparisonResult] =
    useState<EvalCompareDoneData | null>(null);
  const [streamError, setStreamError] = useState<EvalCompareErrorData | null>(
    null,
  );
  const [expandedItemDeltas, setExpandedItemDeltas] = useState<
    Record<string, boolean>
  >({});
  const [taskNotice, setTaskNotice] = useState<string | null>(null);

  useEffect(() => {
    setIsRunning(false);
    setCurrentStage(null);
    setArmOutcomes({});
    setComparisonResult(null);
    setStreamError(null);
    setTaskNotice(null);
  }, [spaceId, isOpen]);

  const stopComparison = () => {
    cancelTask();
    setIsRunning(false);
    setCurrentStage(null);
    setTaskNotice(
      '对比请求已停止。已完成的方案可能已保存，可刷新评测历史确认。',
    );
  };

  const closeDialog = () => {
    if (taskRunning()) {
      stopComparison();
      toast.info('已停止对比请求，可刷新评测历史确认已保存的结果。');
    }
    onClose();
  };

  const handleAddArm = () => {
    if (arms.length >= 4) {
      toast.warning('多臂对比最多支持 4 臂配置');
      return;
    }
    const newIdx = arms.length;
    const defaultLabels = [
      '关掉多样性',
      '增大重排数(15)',
      '开启 HyDE 假设检索',
    ];
    const suggestedLabel = defaultLabels[newIdx - 1] || `方案 ${newIdx}`;

    const newArm: ArmConfig = {
      id: `arm-${Date.now()}`,
      label: suggestedLabel,
      enabledKnobs: {
        top_n_rerank: true,
      },
      overrides: {
        top_n_rerank: 15,
      },
      showAdvanced: false,
    };
    setArms((prev) => [...prev, newArm]);
  };

  const handleRemoveArm = (index: number) => {
    if (index === 0) return; // Cannot delete baseline
    if (arms.length <= 2) {
      toast.warning('多臂对比至少需要 2 臂（1 臂基准 + 1 臂对比）');
      return;
    }
    setArms((prev) => prev.filter((_, idx) => idx !== index));
  };

  const handleUpdateLabel = (index: number, label: string) => {
    setArms((prev) =>
      prev.map((arm, idx) => (idx === index ? { ...arm, label } : arm)),
    );
  };

  const handleToggleKnob = (
    index: number,
    knob: keyof ArmConfig['enabledKnobs'],
    enabled: boolean,
  ) => {
    setArms((prev) =>
      prev.map((arm, idx) => {
        if (idx !== index) return arm;
        const nextEnabled = { ...arm.enabledKnobs, [knob]: enabled };
        const nextOverrides = { ...arm.overrides };
        if (enabled && nextOverrides[knob] === undefined) {
          // Supply a sensible default
          if (knob === 'diversity') nextOverrides.diversity = false;
          else if (knob === 'top_n_rerank') nextOverrides.top_n_rerank = 8;
          else if (knob === 'top_k_vector') nextOverrides.top_k_vector = 50;
          else if (knob === 'top_k_fts') nextOverrides.top_k_fts = 50;
          else if (knob === 'max_insights') nextOverrides.max_insights = 6;
          else if (knob === 'hyde') nextOverrides.hyde = true;
          else if (knob === 'mmr_lambda') nextOverrides.mmr_lambda = 0.7;
          else if (knob === 'dedup_threshold')
            nextOverrides.dedup_threshold = 0.85;
        }
        return {
          ...arm,
          enabledKnobs: nextEnabled,
          overrides: nextOverrides,
        };
      }),
    );
  };

  const handleUpdateKnobValue = <K extends keyof ArmConfig['overrides']>(
    index: number,
    knob: K,
    value: ArmConfig['overrides'][K],
  ) => {
    setArms((prev) =>
      prev.map((arm, idx) => {
        if (idx !== index) return arm;
        return {
          ...arm,
          overrides: {
            ...arm.overrides,
            [knob]: value,
          },
        };
      }),
    );
  };

  const toggleAdvanced = (index: number) => {
    setArms((prev) =>
      prev.map((arm, idx) =>
        idx === index ? { ...arm, showAdvanced: !arm.showAdvanced } : arm,
      ),
    );
  };

  const toggleItemDeltas = (label: string) => {
    setExpandedItemDeltas((prev) => ({
      ...prev,
      [label]: !prev[label],
    }));
  };

  const runCompare = useCallback(async () => {
    if (!spaceId || !isOpen) return;
    if (evalItemCount === 0) {
      toast.error('当前评测集题目为空，无法进行多臂对比');
      return;
    }
    const task = startTask();
    if (!task) return;
    let terminal = false;

    setIsRunning(true);
    setTaskNotice(null);
    setStreamError(null);
    setCurrentStage(null);
    setArmOutcomes({});
    setComparisonResult(null);

    // Build payload according to backend API spec
    const requestPayload: EvalCompareRequest = {
      arms: arms.map((arm, idx) => {
        if (idx === 0) {
          return {
            label: arm.label.trim() || '基准',
            retrieval: null,
            insight_set: [],
          };
        }
        const retrieval: RetrievalKnobOverrides = {};
        if (
          arm.enabledKnobs.diversity &&
          typeof arm.overrides.diversity === 'boolean'
        ) {
          retrieval.diversity = arm.overrides.diversity;
        }
        if (
          arm.enabledKnobs.top_n_rerank &&
          typeof arm.overrides.top_n_rerank === 'number'
        ) {
          retrieval.top_n_rerank = arm.overrides.top_n_rerank;
        }
        if (
          arm.enabledKnobs.top_k_vector &&
          typeof arm.overrides.top_k_vector === 'number'
        ) {
          retrieval.top_k_vector = arm.overrides.top_k_vector;
        }
        if (
          arm.enabledKnobs.top_k_fts &&
          typeof arm.overrides.top_k_fts === 'number'
        ) {
          retrieval.top_k_fts = arm.overrides.top_k_fts;
        }
        if (
          arm.enabledKnobs.max_insights &&
          typeof arm.overrides.max_insights === 'number'
        ) {
          retrieval.max_insights = arm.overrides.max_insights;
        }
        if (arm.enabledKnobs.hyde && typeof arm.overrides.hyde === 'boolean') {
          retrieval.hyde = arm.overrides.hyde;
        }
        if (
          arm.enabledKnobs.mmr_lambda &&
          typeof arm.overrides.mmr_lambda === 'number'
        ) {
          retrieval.mmr_lambda = arm.overrides.mmr_lambda;
        }
        if (
          arm.enabledKnobs.dedup_threshold &&
          typeof arm.overrides.dedup_threshold === 'number'
        ) {
          retrieval.dedup_threshold = arm.overrides.dedup_threshold;
        }

        return {
          label: arm.label.trim() || `方案 ${idx}`,
          retrieval: Object.keys(retrieval).length > 0 ? retrieval : null,
          insight_set: [],
        };
      }),
      persist: true,
    };

    try {
      await expertiseService.compareEvalsStream(
        spaceId,
        requestPayload,
        {
          onStage: (data) => {
            if (!task.current() || terminal) return;
            setCurrentStage(data);
          },
          onArm: (data) => {
            if (!task.current() || terminal) return;
            setArmOutcomes((prev) => ({
              ...prev,
              [data.label]: data,
            }));
          },
          onDone: (data) => {
            if (!task.current() || terminal) return;
            terminal = true;
            setIsRunning(false);
            setCurrentStage(null);
            setComparisonResult(data);
            toast.success(
              `多臂对比完成！对比了 ${data.deltas.length + 1} 个检索配置`,
            );
            onComparisonComplete?.();
          },
          onError: (err) => {
            if (!task.current() || terminal) return;
            terminal = true;
            setIsRunning(false);
            setCurrentStage(null);
            setStreamError(err);
            toast.error(err.message || '多臂对比流式服务异常');
          },
        },
        task.signal,
      );
      if (task.current() && !terminal) {
        setStreamError({
          code: 'INCOMPLETE_STREAM',
          message: '对比连接已结束，未收到完成结果。请刷新评测历史后确认。',
        });
      }
    } catch (err: unknown) {
      if (task.current() && !terminal) {
        setStreamError({
          code: 'REQUEST_FAILED',
          message: (err as Error)?.message || '发起多臂对比请求失败',
        });
        toast.error('发起多臂对比失败');
      }
    } finally {
      if (task.finish()) {
        setIsRunning(false);
        setCurrentStage(null);
      }
    }
  }, [spaceId, isOpen, startTask, evalItemCount, arms, onComparisonComplete]);

  return (
    <Dialog open={isOpen} onOpenChange={(open) => !open && closeDialog()}>
      <DialogContent className="sm:max-w-4xl max-h-[90vh] flex flex-col p-0 gap-0 overflow-hidden bg-card border-border">
        {/* 对话框头部 */}
        <DialogHeader className="px-6 py-4 border-b border-border/80 shrink-0 bg-muted/20">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex flex-wrap items-center gap-2 pr-6">
              <div className="p-1.5 rounded-lg bg-primary/10 text-primary">
                <GitCompare className="h-5 w-5" />
              </div>
              <div>
                <DialogTitle className="text-base font-semibold text-foreground">
                  检索配置对比
                </DialogTitle>
                <DialogDescription className="text-xs text-muted-foreground mt-0.5">
                  固定考题配对比较不同检索旋钮的得分与召回指标变化。
                </DialogDescription>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={handleAddArm}
                disabled={isRunning || arms.length >= 4}
                className="h-8 gap-1.5 text-xs"
              >
                <Plus className="h-3.5 w-3.5" />
                添加方案臂 ({arms.length}/4)
              </Button>
              {isRunning && (
                <Button
                  variant="destructive"
                  size="sm"
                  onClick={stopComparison}
                  className="h-8 gap-1.5 text-xs"
                >
                  <Square className="h-3.5 w-3.5" />
                  停止对比
                </Button>
              )}
              <Button
                size="sm"
                onClick={runCompare}
                disabled={isRunning || evalItemCount === 0}
                className="h-8 gap-1.5 text-xs bg-primary text-primary-foreground hover:bg-primary/90"
              >
                {isRunning ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Play className="h-3.5 w-3.5 fill-current" />
                )}
                {isRunning ? '正在对比...' : `开始多臂对比 (${arms.length} 臂)`}
              </Button>
            </div>
          </div>
        </DialogHeader>

        {/* 主体滚动区 */}
        <div className="flex-1 overflow-y-auto p-6 space-y-5">
          {/* A/A 噪声底提示条（强约束提示） */}
          <div className="flex items-start gap-2.5 rounded-xl border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-900 dark:text-amber-200 leading-relaxed">
            <AlertTriangle className="h-4 w-4 shrink-0 text-amber-700 dark:text-amber-400 mt-0.5" />
            <div>
              <span className="font-semibold">A/A 对照与噪声底准则：</span>
              同一配置跑两臂即为 A/A
              对照（噪声底）。低于噪声底的差值不值得当结论；必须在同一批测试集上配对相减才具备统计意义。
            </div>
          </div>

          {/* 流内错误展示（后端 event: error 或网络异常） */}
          {streamError && (
            <div
              role="alert"
              className="flex items-start gap-2.5 rounded-xl border border-destructive/40 bg-destructive/10 p-3.5 text-xs text-destructive"
            >
              <AlertCircle className="h-4 w-4 shrink-0 mt-0.5" />
              <div className="space-y-1 flex-1">
                <div className="font-semibold flex items-center justify-between">
                  <span>对比执行出错</span>
                  {streamError.code && (
                    <span className="font-mono text-[10px] px-2 py-0.5 rounded bg-destructive/20 uppercase">
                      {streamError.code}
                    </span>
                  )}
                </div>
                <p className="text-foreground/90 font-mono text-[11px] leading-relaxed">
                  {streamError.message}
                </p>
                {streamError.detail != null && (
                  <pre className="text-[10px] bg-background/50 p-2 rounded border border-destructive/20 overflow-x-auto">
                    {typeof streamError.detail === 'string'
                      ? streamError.detail
                      : JSON.stringify(streamError.detail, null, 2)}
                  </pre>
                )}
              </div>
            </div>
          )}

          {taskNotice && (
            <p
              role="status"
              className="rounded-xl border border-border bg-muted/30 p-3.5 text-xs text-muted-foreground"
            >
              {taskNotice}
            </p>
          )}

          {/* 运行中进度条 */}
          {isRunning && (
            <div
              role="status"
              className="rounded-xl border border-primary/30 bg-primary/5 p-3.5 flex flex-wrap gap-2 items-center justify-between text-xs"
            >
              <div className="flex items-center gap-2">
                <Loader2 className="h-4 w-4 animate-spin text-primary" />
                <span className="font-medium text-foreground">
                  正在执行多臂流水线:
                </span>
                <span className="font-mono text-primary font-semibold">
                  {currentStage?.label
                    ? `[${currentStage.label}]`
                    : '准备中...'}
                </span>
              </div>
              <span className="text-muted-foreground text-[11px] font-mono">
                停止或关闭弹窗会中止请求，已保存的记录会保留
              </span>
            </div>
          )}

          {/* 对比结果卡片（如果已产生结果） */}
          {comparisonResult && (
            <div className="rounded-2xl border border-primary/30 bg-primary/[0.03] p-4.5 space-y-3.5">
              <div className="flex items-center justify-between border-b border-border/60 pb-2.5">
                <div className="flex items-center gap-2">
                  <CheckCircle2 className="h-4 w-4 text-emerald-700 dark:text-emerald-400" />
                  <h4 className="text-xs font-semibold text-foreground">
                    对比结果
                  </h4>
                  <span className="font-mono text-[10px] text-muted-foreground bg-muted px-2 py-0.5 rounded">
                    考题数: {comparisonResult.items}
                  </span>
                </div>
                <span className="text-[11px] text-muted-foreground">
                  基准臂：
                  <strong className="text-foreground font-mono">
                    {comparisonResult.baseline}
                  </strong>
                </span>
              </div>

              {/* 基准臂单行卡片 */}
              {(() => {
                const baseOutcome = armOutcomes[comparisonResult.baseline];
                return (
                  <div className="p-3 rounded-xl border border-border/80 bg-background/80 space-y-2">
                    <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
                      <div className="flex items-center gap-2">
                        <span className="px-2 py-0.5 rounded-md text-[10px] font-semibold bg-primary/10 text-primary border border-primary/30">
                          基准配置
                        </span>
                        <span className="font-semibold text-xs text-foreground">
                          {comparisonResult.baseline}
                        </span>
                        {baseOutcome?.run_id && (
                          <span className="font-mono text-[10px] text-muted-foreground">
                            ID: {baseOutcome.run_id.slice(0, 8)}
                          </span>
                        )}
                      </div>

                      <div className="flex items-center gap-2 font-mono">
                        <span className="text-[11px] text-muted-foreground">
                          基准得分:
                        </span>
                        <span className="text-sm font-bold text-foreground">
                          {baseOutcome?.score != null
                            ? baseOutcome.score.toFixed(1)
                            : '-'}
                        </span>
                      </div>
                    </div>

                    <div className="pt-1.5 border-t border-border/40 flex items-center justify-between">
                      <RetrievalMetricsView
                        metrics={baseOutcome?.metrics}
                        compact
                        showAuditDetails
                      />
                    </div>
                  </div>
                );
              })()}

              {/* 各对比臂结果行（包含总分、差值、三项检索差值、逐题差值折叠） */}
              <div className="space-y-2.5">
                {comparisonResult.deltas.map((delta) => {
                  const outcome = armOutcomes[delta.label];
                  const isExpanded = Boolean(expandedItemDeltas[delta.label]);
                  const scoreDelta = delta.score_delta;
                  const isSignificant = delta.significant === true;
                  const isPositive = scoreDelta > 0 && isSignificant;
                  const isNegative = scoreDelta < 0 && isSignificant;

                  const allScoreDeltas = comparisonResult.deltas.map((d) =>
                    Math.abs(d.score_delta || 0),
                  );
                  const maxScoreDelta = Math.max(5, ...allScoreDeltas);

                  // 只有这三项是真的：召回 / 排序精度 / 忠实度。别给它们套上
                  // Hit Rate / MRR / NDCG 之类的名字——那些是另外的指标，
                  // 标签与算法说的不是一回事比没有这个数字更糟
                  const recallDelta = delta.metrics_delta?.context_recall;
                  const precisionDelta = delta.metrics_delta?.context_precision;
                  const faithfulnessDelta = delta.metrics_delta?.faithfulness;

                  const metricDeltas = [
                    Math.abs(recallDelta || 0),
                    Math.abs(precisionDelta || 0),
                    Math.abs(faithfulnessDelta || 0),
                  ];
                  const maxMetricDelta = Math.max(0.1, ...metricDeltas);

                  const maxItemDelta = Math.max(
                    2,
                    ...(delta.item_deltas?.map((it) =>
                      Math.abs(it.score_delta),
                    ) || [2]),
                  );

                  return (
                    <div
                      key={delta.label}
                      className="p-3.5 rounded-xl border border-border/80 bg-background/80 space-y-3 transition-colors"
                    >
                      {/* 臂头部信息 */}
                      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2 border-b border-border/40 pb-2.5">
                        <div className="flex items-center gap-2">
                          <span className="px-2 py-0.5 rounded-md text-[10px] font-medium bg-muted text-muted-foreground border border-border/80">
                            对比臂
                          </span>
                          <span className="font-semibold text-xs text-foreground">
                            {delta.label}
                          </span>
                          {outcome?.run_id && (
                            <span className="font-mono text-[10px] text-muted-foreground">
                              ID: {outcome.run_id.slice(0, 8)}
                            </span>
                          )}
                        </div>

                        <div className="flex items-center gap-3">
                          <div className="flex items-center gap-1.5 font-mono">
                            <span className="text-[11px] text-muted-foreground">
                              得分:
                            </span>
                            <span className="text-xs font-semibold text-foreground">
                              {outcome?.score != null
                                ? outcome.score.toFixed(1)
                                : '-'}
                            </span>
                            <span
                              className={cn(
                                'text-xs font-bold px-1.5 py-0.2 rounded border tabular-nums',
                                isPositive
                                  ? 'text-emerald-700 dark:text-emerald-400 bg-emerald-500/10 border-emerald-500/25'
                                  : isNegative
                                    ? 'text-destructive bg-destructive/10 border-destructive/25'
                                    : 'text-muted-foreground bg-muted border-border/60',
                              )}
                            >
                              Δ{' '}
                              {scoreDelta > 0
                                ? `+${scoreDelta.toFixed(1)}`
                                : scoreDelta.toFixed(1)}
                            </span>
                            <span
                              className="text-[10px] text-muted-foreground"
                              title="逐题配对 t 检验，|t| 超过临界值才算显著（双侧约 95%）"
                            >
                              {delta.significant === undefined
                                ? '未检验'
                                : isSignificant
                                  ? `显著 · t=${delta.t_stat?.toFixed(1)}`
                                  : `噪声内 · t=${delta.t_stat?.toFixed(1) ?? '—'}`}
                              {delta.paired_items
                                ? ` · ${delta.paired_items} 题配对`
                                : ''}
                            </span>
                          </div>

                          {delta.item_deltas &&
                            delta.item_deltas.length > 0 && (
                              <Button
                                variant="ghost"
                                size="sm"
                                onClick={() => toggleItemDeltas(delta.label)}
                                className="h-6 px-2 text-[11px] text-muted-foreground gap-1 hover:text-foreground cursor-pointer"
                              >
                                <span>
                                  {isExpanded
                                    ? '收起逐题差值'
                                    : `逐题差值 (${delta.item_deltas.length} 题)`}
                                </span>
                                {isExpanded ? (
                                  <ChevronUp className="h-3 w-3" />
                                ) : (
                                  <ChevronDown className="h-3 w-3" />
                                )}
                              </Button>
                            )}
                        </div>
                      </div>

                      {/* 核心可视化 1：总分差值横向条形对比（横轴以 0 为中心） */}
                      <div className="rounded-lg border border-border/60 bg-muted/20 p-2.5 space-y-1">
                        <div className="flex items-center justify-between text-[11px] font-medium text-muted-foreground pb-0.5">
                          <span>总分差值</span>
                          <span className="font-mono text-[10px] text-muted-foreground">
                            以基准臂为 0 基线
                          </span>
                        </div>
                        <BipolarDeltaBar
                          label="Δ Score"
                          value={scoreDelta}
                          maxVal={maxScoreDelta}
                          digits={1}
                          tooltipTitle="总分差值 (Δ Score)"
                          significant={delta.significant}
                          tooltipDesc="对比臂相较基准臂的得分差值。正值代表综合表现超越基准，负值代表回退。"
                        />
                      </div>

                      {/* 核心可视化 2：三项检索指标差值横向条形对比（横轴以 0 为中心，严格保留有符号三位小数） */}
                      <div className="rounded-lg border border-border/60 bg-muted/20 p-2.5 space-y-1.5">
                        <div className="flex items-center justify-between text-[11px] font-medium text-muted-foreground border-b border-border/40 pb-1">
                          <span>检索指标差值</span>
                          <span className="font-mono text-[10px] text-muted-foreground">
                            横轴中心为 0 · 有符号三位小数
                          </span>
                        </div>
                        <BipolarDeltaBar
                          label="召回差"
                          value={recallDelta}
                          maxVal={maxMetricDelta}
                          digits={3}
                          tooltipTitle="检索召回差值 (Δ context recall)"
                          significant={
                            delta.metrics_significant?.context_recall
                          }
                          tooltipDesc="参考答案要点中被检索到的证据支撑的比例之差。偏低说明证据没捞到。"
                        />
                        <BipolarDeltaBar
                          label="精度差"
                          value={precisionDelta}
                          maxVal={maxMetricDelta}
                          digits={3}
                          tooltipTitle="排序精度差值 (Δ context precision)"
                          significant={
                            delta.metrics_significant?.context_precision
                          }
                          tooltipDesc="被用到的证据在检索结果里排得靠前不靠前之差。偏低说明排序 / 重排有问题。"
                        />
                        <BipolarDeltaBar
                          label="忠实差"
                          value={faithfulnessDelta}
                          maxVal={maxMetricDelta}
                          digits={3}
                          tooltipTitle="答案忠实度差值 (Δ faithfulness)"
                          significant={delta.metrics_significant?.faithfulness}
                          tooltipDesc="答案论断中能在证据里找到依据的比例之差。偏低说明模型没用上证据。"
                        />
                      </div>

                      {/* 核心可视化 3：展开逐题配对得分差值小条形列表 */}
                      {isExpanded &&
                        delta.item_deltas &&
                        delta.item_deltas.length > 0 && (
                          <div className="mt-2 pt-2 border-t border-border/40 space-y-2">
                            <div className="flex items-center justify-between text-[11px] font-medium text-muted-foreground">
                              <span>
                                逐题配对得分差值 (共 {delta.item_deltas.length}{' '}
                                题)
                              </span>
                              <span className="text-[10px]">
                                单题差值随机波动约 ±15 分，不要逐题下结论
                              </span>
                            </div>
                            <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-2">
                              {delta.item_deltas.map((it, itemIdx) => (
                                <ItemDeltaMiniBar
                                  key={it.item_id || itemIdx}
                                  index={itemIdx}
                                  scoreDelta={it.score_delta}
                                  maxAbsScore={maxItemDelta}
                                />
                              ))}
                            </div>
                          </div>
                        )}
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {/* 臂配置编辑区 */}
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <h4 className="text-xs font-semibold text-foreground flex items-center gap-1.5">
                <Sliders className="h-3.5 w-3.5 text-primary" />
                多臂检索参数覆盖配置 (2~4 臂)
              </h4>
              <span className="text-[11px] text-muted-foreground">
                第一臂固定为基准（空间默认配置）；其余各臂仅覆写选中的旋钮。
              </span>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-3.5">
              {arms.map((arm, index) => {
                const isBaseline = index === 0;

                return (
                  <div
                    key={arm.id}
                    className={cn(
                      'rounded-xl border p-4 text-xs space-y-3 relative transition-all',
                      isBaseline
                        ? 'border-primary/40 bg-primary/[0.02]'
                        : 'border-border/80 bg-card shadow-2xs',
                    )}
                  >
                    {/* 臂标题与删除 */}
                    <div className="flex items-center justify-between gap-2">
                      <div className="flex items-center gap-2 flex-1">
                        <span
                          className={cn(
                            'font-mono text-[10px] font-semibold px-2 py-0.5 rounded',
                            isBaseline
                              ? 'bg-primary/10 text-primary border border-primary/25'
                              : 'bg-muted text-muted-foreground border border-border/80',
                          )}
                        >
                          Arm #{index + 1}
                        </span>
                        {isBaseline ? (
                          <span className="font-semibold text-foreground text-xs">
                            {arm.label}
                          </span>
                        ) : (
                          <Input
                            value={arm.label}
                            onChange={(e) =>
                              handleUpdateLabel(index, e.target.value)
                            }
                            disabled={isRunning}
                            placeholder="给该方案命名，如「关掉多样性」"
                            className="h-7 text-xs flex-1"
                          />
                        )}
                      </div>

                      {!isBaseline && arms.length > 2 && (
                        <Button
                          variant="ghost"
                          size="icon-xs"
                          onClick={() => handleRemoveArm(index)}
                          disabled={isRunning}
                          className="h-6 w-6 text-muted-foreground hover:text-destructive"
                          title="删除该臂"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button>
                      )}
                    </div>

                    {isBaseline ? (
                      <div className="py-4 text-center rounded-lg border border-dashed border-primary/25 bg-primary/5 text-muted-foreground space-y-1">
                        <div className="font-medium text-xs text-foreground flex items-center justify-center gap-1.5">
                          <Sparkles className="h-3.5 w-3.5 text-primary" />
                          基准配置（空间全局默认）
                        </div>
                        <p className="text-[10.5px] text-muted-foreground">
                          不覆写任何检索旋钮，作为对照组衡量其余各臂的绝对得失。
                        </p>
                      </div>
                    ) : (
                      <div className="space-y-3 pt-1">
                        {/* 常用核心旋钮：diversity 与 top_n_rerank */}
                        <div className="space-y-2.5 rounded-lg border border-border/60 bg-muted/20 p-2.5">
                          <div className="text-[11px] font-semibold text-foreground flex items-center justify-between">
                            <span>常用检索参数</span>
                            <span className="text-[10px] text-muted-foreground font-normal">
                              勾选即覆盖
                            </span>
                          </div>

                          {/* 旋钮 1: diversity */}
                          <div className="flex items-center justify-between gap-3 text-[11px]">
                            <div className="flex items-center gap-2">
                              <input
                                type="checkbox"
                                id={`arm-${index}-div-check`}
                                checked={Boolean(arm.enabledKnobs.diversity)}
                                onChange={(e) =>
                                  handleToggleKnob(
                                    index,
                                    'diversity',
                                    e.target.checked,
                                  )
                                }
                                disabled={isRunning}
                                className="rounded text-primary focus:ring-primary h-3.5 w-3.5"
                              />
                              <label
                                htmlFor={`arm-${index}-div-check`}
                                className="font-medium text-foreground cursor-pointer select-none"
                              >
                                多样性总开关 (diversity)
                              </label>
                            </div>

                            {arm.enabledKnobs.diversity && (
                              <div className="flex items-center gap-1.5">
                                <span className="text-[10px] text-muted-foreground font-mono">
                                  {arm.overrides.diversity ? '开启' : '关闭'}
                                </span>
                                <Switch
                                  checked={Boolean(arm.overrides.diversity)}
                                  onCheckedChange={(checked) =>
                                    handleUpdateKnobValue(
                                      index,
                                      'diversity',
                                      checked,
                                    )
                                  }
                                  disabled={isRunning}
                                  size="sm"
                                />
                              </div>
                            )}
                          </div>

                          {/* 旋钮 2: top_n_rerank */}
                          <div className="flex items-center justify-between gap-3 text-[11px]">
                            <div className="flex items-center gap-2">
                              <input
                                type="checkbox"
                                id={`arm-${index}-rerank-check`}
                                checked={Boolean(arm.enabledKnobs.top_n_rerank)}
                                onChange={(e) =>
                                  handleToggleKnob(
                                    index,
                                    'top_n_rerank',
                                    e.target.checked,
                                  )
                                }
                                disabled={isRunning}
                                className="rounded text-primary focus:ring-primary h-3.5 w-3.5"
                              />
                              <label
                                htmlFor={`arm-${index}-rerank-check`}
                                className="font-medium text-foreground cursor-pointer select-none"
                              >
                                重排保留切片数 (top_n_rerank)
                              </label>
                            </div>

                            {arm.enabledKnobs.top_n_rerank && (
                              <div className="flex items-center gap-1">
                                <Input
                                  type="number"
                                  min={1}
                                  max={50}
                                  value={arm.overrides.top_n_rerank ?? 8}
                                  onChange={(e) =>
                                    handleUpdateKnobValue(
                                      index,
                                      'top_n_rerank',
                                      parseInt(e.target.value, 10) || 1,
                                    )
                                  }
                                  disabled={isRunning}
                                  className="h-6 w-16 text-xs text-right font-mono"
                                />
                              </div>
                            )}
                          </div>
                        </div>

                        {/* 高级旋钮折叠区 */}
                        <div>
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => toggleAdvanced(index)}
                            disabled={isRunning}
                            className="h-6 px-1.5 text-[11px] text-muted-foreground gap-1 hover:text-foreground w-full justify-between"
                          >
                            <span>
                              高级检索旋钮 (向量/全文Top-K、MMR权重、HyDE等)
                            </span>
                            {arm.showAdvanced ? (
                              <ChevronUp className="h-3 w-3" />
                            ) : (
                              <ChevronDown className="h-3 w-3" />
                            )}
                          </Button>

                          {arm.showAdvanced && (
                            <div className="mt-2 space-y-2 p-2.5 rounded-lg border border-border/50 bg-muted/10 text-[11px]">
                              {/* top_k_vector */}
                              <div className="flex items-center justify-between gap-2">
                                <div className="flex items-center gap-1.5">
                                  <input
                                    type="checkbox"
                                    id={`arm-${index}-kvec-check`}
                                    checked={Boolean(
                                      arm.enabledKnobs.top_k_vector,
                                    )}
                                    onChange={(e) =>
                                      handleToggleKnob(
                                        index,
                                        'top_k_vector',
                                        e.target.checked,
                                      )
                                    }
                                    disabled={isRunning}
                                    className="rounded text-primary h-3 w-3"
                                  />
                                  <label htmlFor={`arm-${index}-kvec-check`}>
                                    向量召回候选数 (top_k_vector)
                                  </label>
                                </div>
                                {arm.enabledKnobs.top_k_vector && (
                                  <Input
                                    type="number"
                                    min={1}
                                    max={200}
                                    value={arm.overrides.top_k_vector ?? 50}
                                    onChange={(e) =>
                                      handleUpdateKnobValue(
                                        index,
                                        'top_k_vector',
                                        parseInt(e.target.value, 10) || 1,
                                      )
                                    }
                                    disabled={isRunning}
                                    className="h-6 w-16 text-xs text-right font-mono"
                                  />
                                )}
                              </div>

                              {/* top_k_fts */}
                              <div className="flex items-center justify-between gap-2">
                                <div className="flex items-center gap-1.5">
                                  <input
                                    type="checkbox"
                                    id={`arm-${index}-kfts-check`}
                                    checked={Boolean(
                                      arm.enabledKnobs.top_k_fts,
                                    )}
                                    onChange={(e) =>
                                      handleToggleKnob(
                                        index,
                                        'top_k_fts',
                                        e.target.checked,
                                      )
                                    }
                                    disabled={isRunning}
                                    className="rounded text-primary h-3 w-3"
                                  />
                                  <label htmlFor={`arm-${index}-kfts-check`}>
                                    全文召回候选数 (top_k_fts)
                                  </label>
                                </div>
                                {arm.enabledKnobs.top_k_fts && (
                                  <Input
                                    type="number"
                                    min={1}
                                    max={200}
                                    value={arm.overrides.top_k_fts ?? 50}
                                    onChange={(e) =>
                                      handleUpdateKnobValue(
                                        index,
                                        'top_k_fts',
                                        parseInt(e.target.value, 10) || 1,
                                      )
                                    }
                                    disabled={isRunning}
                                    className="h-6 w-16 text-xs text-right font-mono"
                                  />
                                )}
                              </div>

                              {/* max_insights */}
                              <div className="flex items-center justify-between gap-2">
                                <div className="flex items-center gap-1.5">
                                  <input
                                    type="checkbox"
                                    id={`arm-${index}-maxins-check`}
                                    checked={Boolean(
                                      arm.enabledKnobs.max_insights,
                                    )}
                                    onChange={(e) =>
                                      handleToggleKnob(
                                        index,
                                        'max_insights',
                                        e.target.checked,
                                      )
                                    }
                                    disabled={isRunning}
                                    className="rounded text-primary h-3 w-3"
                                  />
                                  <label htmlFor={`arm-${index}-maxins-check`}>
                                    经验最大召回数 (max_insights)
                                  </label>
                                </div>
                                {arm.enabledKnobs.max_insights && (
                                  <Input
                                    type="number"
                                    min={0}
                                    max={20}
                                    value={arm.overrides.max_insights ?? 6}
                                    onChange={(e) =>
                                      handleUpdateKnobValue(
                                        index,
                                        'max_insights',
                                        parseInt(e.target.value, 10) || 0,
                                      )
                                    }
                                    disabled={isRunning}
                                    className="h-6 w-16 text-xs text-right font-mono"
                                  />
                                )}
                              </div>

                              {/* hyde */}
                              <div className="flex items-center justify-between gap-2">
                                <div className="flex items-center gap-1.5">
                                  <input
                                    type="checkbox"
                                    id={`arm-${index}-hyde-check`}
                                    checked={Boolean(arm.enabledKnobs.hyde)}
                                    onChange={(e) =>
                                      handleToggleKnob(
                                        index,
                                        'hyde',
                                        e.target.checked,
                                      )
                                    }
                                    disabled={isRunning}
                                    className="rounded text-primary h-3 w-3"
                                  />
                                  <label htmlFor={`arm-${index}-hyde-check`}>
                                    假设文档生成 (hyde)
                                  </label>
                                </div>
                                {arm.enabledKnobs.hyde && (
                                  <Switch
                                    checked={Boolean(arm.overrides.hyde)}
                                    onCheckedChange={(checked) =>
                                      handleUpdateKnobValue(
                                        index,
                                        'hyde',
                                        checked,
                                      )
                                    }
                                    disabled={isRunning}
                                    size="sm"
                                  />
                                )}
                              </div>

                              {/* mmr_lambda */}
                              <div className="flex items-center justify-between gap-2">
                                <div className="flex items-center gap-1.5">
                                  <input
                                    type="checkbox"
                                    id={`arm-${index}-lambda-check`}
                                    checked={Boolean(
                                      arm.enabledKnobs.mmr_lambda,
                                    )}
                                    onChange={(e) =>
                                      handleToggleKnob(
                                        index,
                                        'mmr_lambda',
                                        e.target.checked,
                                      )
                                    }
                                    disabled={isRunning}
                                    className="rounded text-primary h-3 w-3"
                                  />
                                  <label htmlFor={`arm-${index}-lambda-check`}>
                                    MMR 相关性权重 (mmr_lambda, 0~1)
                                  </label>
                                </div>
                                {arm.enabledKnobs.mmr_lambda && (
                                  <Input
                                    type="number"
                                    step="0.05"
                                    min={0}
                                    max={1}
                                    value={arm.overrides.mmr_lambda ?? 0.7}
                                    onChange={(e) =>
                                      handleUpdateKnobValue(
                                        index,
                                        'mmr_lambda',
                                        parseFloat(e.target.value) || 0.7,
                                      )
                                    }
                                    disabled={isRunning}
                                    className="h-6 w-16 text-xs text-right font-mono"
                                  />
                                )}
                              </div>

                              {/* dedup_threshold */}
                              <div className="flex items-center justify-between gap-2">
                                <div className="flex items-center gap-1.5">
                                  <input
                                    type="checkbox"
                                    id={`arm-${index}-dedup-check`}
                                    checked={Boolean(
                                      arm.enabledKnobs.dedup_threshold,
                                    )}
                                    onChange={(e) =>
                                      handleToggleKnob(
                                        index,
                                        'dedup_threshold',
                                        e.target.checked,
                                      )
                                    }
                                    disabled={isRunning}
                                    className="rounded text-primary h-3 w-3"
                                  />
                                  <label htmlFor={`arm-${index}-dedup-check`}>
                                    去重重合阈值 (dedup_threshold, 0~1)
                                  </label>
                                </div>
                                {arm.enabledKnobs.dedup_threshold && (
                                  <Input
                                    type="number"
                                    step="0.05"
                                    min={0}
                                    max={1}
                                    value={
                                      arm.overrides.dedup_threshold ?? 0.85
                                    }
                                    onChange={(e) =>
                                      handleUpdateKnobValue(
                                        index,
                                        'dedup_threshold',
                                        parseFloat(e.target.value) || 0.85,
                                      )
                                    }
                                    disabled={isRunning}
                                    className="h-6 w-16 text-xs text-right font-mono"
                                  />
                                )}
                              </div>
                            </div>
                          )}
                        </div>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
