import React, { useState, useEffect, useCallback, lazy, Suspense } from 'react';
import { useSpaceStore } from '@/stores/useSpaceStore';
import {
  expertiseService,
  type EvalRunDoneData,
} from '@/lib/api/services/expertise';
import { useAsyncTask } from '@/hooks/useAsyncTask';
import type { components } from '@/lib/api/types.gen';
import type { EvalRun } from '@/lib/api/types.temp';
import { EvalSetItemRow, type EvalItemRowResult } from './EvalSetItemRow';
import { RetrievalMetricsView } from './RetrievalMetricsView';
const EvalCompareDialog = lazy(() =>
  import('./EvalCompareDialog').then((module) => ({
    default: module.EvalCompareDialog,
  })),
);
import { Button } from '@/components/ui/button';
import {
  Play,
  Loader2,
  Sparkles,
  History,
  ChevronDown,
  ChevronUp,
  Award,
  Info,
  GitCompare,
  RefreshCw,
} from 'lucide-react';
import { toast } from 'sonner';
import { useConfirm } from '@/components/shared/ConfirmProvider';

type EvalItem = components['schemas']['EvalItem'];

export function EvalSetRunner() {
  const { currentSpaceId } = useSpaceStore();
  const requestConfirmation = useConfirm();
  const evaluationTask = useAsyncTask(currentSpaceId);
  const generationTask = useAsyncTask(currentSpaceId);
  const [evalItems, setEvalItems] = useState<EvalItem[]>([]);
  const [evalResults, setEvalResults] = useState<
    Record<string, EvalItemRowResult>
  >({});
  const [lastRun, setLastRun] = useState<EvalRunDoneData | null>(null);
  const [evalRuns, setEvalRuns] = useState<EvalRun[]>([]);
  const [isHistoryLoading, setIsHistoryLoading] = useState(false);
  const [expandedRunId, setExpandedRunId] = useState<string | null>(null);
  const [isRunning, setIsRunning] = useState(false);
  const [isGenerating, setIsGenerating] = useState(false);
  const [runningIdx, setRunningIdx] = useState(-1);
  const [isCompareOpen, setIsCompareOpen] = useState(false);
  const [evalsLoadFailed, setEvalsLoadFailed] = useState(false);
  const [runsLoadFailed, setRunsLoadFailed] = useState(false);
  const [taskNotice, setTaskNotice] = useState<{
    role: 'alert' | 'status';
    message: string;
  } | null>(null);

  const reportTaskError = (message: string) => {
    setTaskNotice({ role: 'alert', message });
    toast.error(message);
  };

  const loadEvals = useCallback(async () => {
    if (!currentSpaceId) return;
    try {
      // 取全量：评测分决定经验晋升/淘汰，漏题会在用户不知情下改变结论
      setEvalItems(await expertiseService.getAllEvals(currentSpaceId));
      setEvalsLoadFailed(false);
    } catch (err: unknown) {
      console.error('Failed to load evals:', err);
      // 取不到 ≠ 没有。原本只打日志，界面于是显示「暂无评测用例，请点击『AI 提炼考题』」——
      // 会推着用户去重新生成一套可能已经存在的评测集，白烧模型调用还可能产生重复。
      setEvalsLoadFailed(true);
    }
  }, [currentSpaceId]);

  const loadEvalRuns = useCallback(async () => {
    if (!currentSpaceId) return;
    setIsHistoryLoading(true);
    try {
      const runs = await expertiseService.getEvalRuns(currentSpaceId);
      setEvalRuns(runs || []);
      setRunsLoadFailed(false);
    } catch (err: unknown) {
      console.error('Failed to load eval runs:', err);
      // 取不到 ≠ 没跑过：原本一律显示「暂无评测历史」
      setRunsLoadFailed(true);
    } finally {
      setIsHistoryLoading(false);
    }
  }, [currentSpaceId]);

  useEffect(() => {
    loadEvals();
    loadEvalRuns();
  }, [loadEvals, loadEvalRuns]);

  const handleDeleteItem = async (item: EvalItem) => {
    if (!currentSpaceId) return;
    try {
      await expertiseService.deleteEvalItem(currentSpaceId, item.id);
      setEvalItems((prev) =>
        prev.filter((existing) => existing.id !== item.id),
      );
      toast.success('已删除这道题');
    } catch (err: unknown) {
      toast.error('删除失败', { description: (err as Error)?.message });
    }
  };

  const runEvaluation = async () => {
    if (!currentSpaceId || evalItems.length === 0 || generationTask.running())
      return;
    const task = evaluationTask.start();
    if (!task) return;
    let terminal = false;
    try {
      // 每道题都要真实回答一次再打一次分，题一多就是几十次模型调用
      const count = evalItems.length;
      const minutes = Math.max(1, Math.round((count * 8) / 60));
      const accepted = await requestConfirmation({
        title: '运行这组评测？',
        description: `共 ${count} 道题，每题回答一次、打分一次，约 ${count * 2} 次模型调用，预计 ${minutes} 分钟左右。`,
        confirmText: '开始评测',
      });
      if (!accepted || !task.current()) {
        return;
      }
      setIsRunning(true);
      setTaskNotice(null);
      setEvalResults({});
      setLastRun(null);
      toast.info('开始评测：逐题提问并打分...');

      try {
        await expertiseService.runEvalsStream(
          currentSpaceId,
          { variant: 'with_insights' },
          {
            onProgress: ({ done }) => {
              if (task.current() && !terminal)
                setRunningIdx(Math.max(0, done - 1));
            },
            onItemResult: (data) => {
              if (!task.current() || terminal) return;
              const key = data.item_id || data.eval_id || '';
              setEvalResults((prev) => ({
                ...prev,
                [key]: {
                  passed: data.passed,
                  score: data.score,
                  reason: data.reason || undefined,
                  metrics: data.metrics,
                },
              }));
            },
            onDone: (data: EvalRunDoneData) => {
              if (!task.current() || terminal) return;
              terminal = true;
              setIsRunning(false);
              setRunningIdx(-1);
              setLastRun(data);
              toast.success(`评测完成：评测运行 ID ${data.run_id.slice(0, 8)}`);
              loadEvalRuns();
            },
            onError: (err) => {
              if (!task.current() || terminal) return;
              terminal = true;
              reportTaskError(err.message || '评测运行中断');
              setIsRunning(false);
              setRunningIdx(-1);
            },
          },
          task.signal,
        );
        if (task.current() && !terminal)
          reportTaskError(
            '评测连接已结束，未收到完成结果。请刷新评测记录后确认。',
          );
      } catch (error: unknown) {
        if (task.current() && !terminal)
          reportTaskError(
            error instanceof Error ? error.message : '发起评测失败',
          );
      }
    } finally {
      if (task.finish()) {
        setIsRunning(false);
        setRunningIdx(-1);
      }
    }
  };

  const handleGenerateQuestions = async () => {
    if (!currentSpaceId || evaluationTask.running()) return;
    const task = generationTask.start();
    if (!task) return;
    let terminal = false;
    setIsGenerating(true);
    setTaskNotice(null);
    toast.info('正在根据资料出题...');

    try {
      await expertiseService.generateEvalsStream(
        currentSpaceId,
        { count: 2 },
        {
          onItem: (item) => {
            if (!task.current() || terminal) return;
            setEvalItems((prev) => [item, ...prev]);
            toast.success(`已生成考题：${item.question.slice(0, 20)}...`);
          },
          onDone: ({ count }) => {
            if (!task.current() || terminal) return;
            terminal = true;
            if (count > 0) {
              toast.success(`已生成 ${count} 道考题`);
            } else {
              toast.warning('这次没有生成考题', {
                description:
                  '请确认空间里有已处理完成的资料；资料太短时模型也可能出不了题。',
              });
            }
          },
          onError: (err) => {
            if (!task.current() || terminal) return;
            terminal = true;
            reportTaskError(err.message || '考题生成失败');
          },
        },
        task.signal,
      );
      if (task.current() && !terminal)
        reportTaskError('考题生成连接中断，请刷新题目列表后确认。');
    } catch (error) {
      if (task.current() && !terminal)
        reportTaskError(
          error instanceof Error ? error.message : '考题生成接口异常',
        );
    } finally {
      if (task.finish()) setIsGenerating(false);
    }
  };

  return (
    <div className="space-y-6">
      {/* 评测集控制台主卡片 */}
      <div className="rounded-2xl border border-border/80 bg-card p-6 shadow-xs space-y-4">
        <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3">
          <div>
            <h3 className="text-sm font-semibold text-foreground">测验题</h3>
            <p className="text-xs text-muted-foreground mt-0.5">
              用固定标准考题检验进化效果，严格隔离训练与推理数据，杜绝过拟合。
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              variant="outline"
              onClick={handleGenerateQuestions}
              disabled={isGenerating || isRunning || !currentSpaceId}
              className="h-8 gap-1.5 text-xs"
            >
              {isGenerating ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin text-primary" />
              ) : (
                <Sparkles className="h-3.5 w-3.5 text-primary" />
              )}
              AI 提炼考题
            </Button>

            {isGenerating && (
              <Button
                size="sm"
                variant="destructive"
                onClick={() => {
                  generationTask.cancel();
                  setIsGenerating(false);
                  setTaskNotice({
                    role: 'status',
                    message:
                      '考题生成请求已停止，已保存的题目会保留。可刷新题目列表确认。',
                  });
                }}
              >
                停止出题
              </Button>
            )}

            <Button
              size="sm"
              variant="outline"
              onClick={() => setIsCompareOpen(true)}
              disabled={isRunning || isGenerating || evalItems.length === 0}
              className="h-8 gap-1.5 text-xs border-primary/30 text-primary hover:bg-primary/10"
            >
              <GitCompare className="h-3.5 w-3.5" />
              检索配置对比
            </Button>

            {isRunning && (
              <Button
                size="sm"
                variant="destructive"
                onClick={() => {
                  evaluationTask.cancel();
                  setIsRunning(false);
                  setRunningIdx(-1);
                  setTaskNotice({
                    role: 'status',
                    message:
                      '评测请求已停止。已产生的逐题结果不代表本轮已完成，可刷新评测历史确认。',
                  });
                  toast.info('评测已中止，可以刷新评测记录确认结果。');
                }}
              >
                停止评测
              </Button>
            )}
            <Button
              size="sm"
              onClick={runEvaluation}
              disabled={isRunning || isGenerating || evalItems.length === 0}
              className="h-8 gap-1.5 text-xs bg-primary text-primary-foreground hover:bg-primary/90"
            >
              {isRunning ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Play className="h-3.5 w-3.5 fill-current" />
              )}
              运行评测（{evalItems.length} 题）
            </Button>
          </div>
        </div>

        {taskNotice && (
          <div
            role={taskNotice.role}
            className={
              taskNotice.role === 'alert'
                ? 'rounded-xl border border-destructive/30 bg-destructive/5 p-3 text-xs text-destructive'
                : 'rounded-xl border border-border bg-muted/30 p-3 text-xs text-muted-foreground'
            }
          >
            {taskNotice.message}
            <button
              type="button"
              className="ml-2 underline"
              onClick={() => {
                void loadEvals();
                void loadEvalRuns();
              }}
            >
              刷新题目与记录
            </button>
          </div>
        )}

        {/* 本次评测整轮汇总面板 */}
        {lastRun && (
          <div className="rounded-xl border border-primary/30 bg-primary/5 p-4 space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-primary/15 pb-2.5">
              <div className="flex items-center gap-2">
                <Award className="h-4 w-4 text-primary" />
                <span className="text-xs font-semibold text-foreground">
                  本轮评测运行汇总
                </span>
                <span className="font-mono text-[11px] text-muted-foreground bg-muted px-2 py-0.5 rounded">
                  Run: {lastRun.run_id.slice(0, 8)}
                </span>
                <span className="font-mono text-[10px] text-primary border border-primary/25 px-1.5 py-0.5 rounded uppercase">
                  {lastRun.variant}
                </span>
              </div>
              <div className="flex items-center gap-1.5 font-mono">
                <span className="text-xs text-muted-foreground">总得分:</span>
                <span className="text-base font-bold text-foreground">
                  {lastRun.score.toFixed(1)}
                </span>
              </div>
            </div>

            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
              <div className="space-y-1">
                <div className="text-[11px] font-medium text-foreground/80">
                  本轮检索指标:
                </div>
                <RetrievalMetricsView
                  metrics={lastRun.metrics}
                  showAuditDetails
                />
              </div>
            </div>

            <div className="flex items-start gap-1.5 text-[10.5px] text-muted-foreground bg-background/60 p-2.5 rounded-lg border border-border/40 leading-relaxed">
              <Info className="h-3.5 w-3.5 text-primary shrink-0 mt-0.5" />
              <span>
                <strong>诊断指导：</strong>
                总分偏低但「检索召回」高，说明切片检索完备，问题在模型对证据的吸收与提示词；「检索召回」偏低则是检索层未命中关键依据；「排序精度」偏低需调优重排
                (Rerank)；「忠实度」偏低说明模型存在自由发挥幻觉。指标为「未测到」表示对应考题无必须包含点或缺少审计。
              </span>
            </div>
          </div>
        )}

        {/* 考题及当次逐题结果列表 */}
        <div className="space-y-3 pt-2">
          {evalItems.length === 0 ? (
            <div className="py-10 text-center text-xs text-muted-foreground">
              {evalsLoadFailed
                ? '无法加载评测用例，请检查后端是否在运行；先不要生成，以免与已有考题重复。'
                : '暂无评测用例，请点击「AI 提炼考题」自动根据当前知识库生成评测集。'}
            </div>
          ) : (
            evalItems.map((item, idx) => (
              <EvalSetItemRow
                key={item.id}
                item={item}
                idx={idx}
                isRunning={isRunning && runningIdx === idx}
                result={evalResults[item.id]}
                onDelete={isRunning ? undefined : handleDeleteItem}
              />
            ))
          )}
        </div>
      </div>

      {/* 历次评测 */}
      <div className="rounded-2xl border border-border/80 bg-card p-6 shadow-xs space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border/40 pb-3">
          <div className="flex items-center gap-2">
            <History className="h-4 w-4 text-primary" />
            <h3 className="text-sm font-semibold text-foreground">评测记录</h3>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-xs font-mono text-muted-foreground">
              {runsLoadFailed
                ? '评测历史加载失败'
                : evalRuns.length > 0
                  ? `共 ${evalRuns.length} 次运行`
                  : '暂无评测历史'}
            </span>
            <Button
              variant="ghost"
              size="sm"
              aria-label="刷新评测历史"
              disabled={isHistoryLoading}
              onClick={() => void loadEvalRuns()}
              className="h-7 gap-1.5 px-2 text-xs"
            >
              <RefreshCw className="h-3 w-3" />
              刷新
            </Button>
          </div>
        </div>

        {isHistoryLoading ? (
          <div className="flex items-center justify-center py-8 gap-2 text-xs text-muted-foreground">
            <Loader2 className="h-3.5 w-3.5 animate-spin text-primary" />
            <span>正在加载评测历史...</span>
          </div>
        ) : evalRuns.length === 0 ? (
          <div className="py-6 text-center text-xs text-muted-foreground">
            {runsLoadFailed
              ? '评测记录暂时无法加载，请点击刷新重试。'
              : '还没有评测记录，运行一次评测后会在这里留档。'}
          </div>
        ) : (
          <div className="space-y-3">
            {evalRuns.map((run) => {
              const detailObj =
                run.detail && !Array.isArray(run.detail)
                  ? run.detail
                  : undefined;
              const detailItems = Array.isArray(run.detail)
                ? run.detail
                : run.detail?.items || [];
              const roundMetrics = detailObj?.metrics;
              const isExpanded = expandedRunId === run.id;
              const runDate = new Date(run.created_at).toLocaleString();

              return (
                <div
                  key={run.id}
                  className="rounded-xl border border-border/70 bg-background/40 p-3.5 text-xs transition-colors space-y-2.5"
                >
                  <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
                    <div className="flex items-center gap-2.5">
                      <span className="font-mono font-medium text-foreground">
                        {run.id.slice(0, 10)}
                      </span>
                      <span className="font-mono text-[10px] text-muted-foreground bg-muted px-1.5 py-0.5 rounded uppercase">
                        {run.variant}
                      </span>
                      <span className="text-[11px] text-muted-foreground">
                        {runDate}
                      </span>
                    </div>

                    <div className="flex items-center gap-3">
                      <div className="flex items-center gap-1 font-mono">
                        <span className="text-muted-foreground text-[11px]">
                          整轮得分:
                        </span>
                        <span className="font-bold text-foreground tabular-nums">
                          {typeof run.score === 'number'
                            ? run.score.toFixed(1)
                            : '-'}
                        </span>
                      </div>

                      {detailItems.length > 0 && (
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() =>
                            setExpandedRunId(isExpanded ? null : run.id)
                          }
                          className="h-7 px-2 text-xs text-muted-foreground gap-1 hover:text-foreground"
                        >
                          <span>
                            {isExpanded
                              ? '收起题明细'
                              : `展开明细 (${detailItems.length}题)`}
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

                  {/* 整轮指标展示 */}
                  <div className="flex flex-wrap items-center gap-2 pt-1 border-t border-border/30">
                    <span className="text-[11px] font-medium text-foreground/80">
                      整轮检索指标:
                    </span>
                    <RetrievalMetricsView
                      metrics={roundMetrics}
                      showAuditDetails
                    />
                  </div>

                  {/* 展开的单题得分与单题指标 */}
                  {isExpanded && detailItems.length > 0 && (
                    <div className="mt-2 pt-2 border-t border-border/40 space-y-2 pl-2">
                      <div className="text-[11px] font-medium text-muted-foreground mb-1">
                        逐题评测明细与指标:
                      </div>
                      {detailItems.map((itemScore, itemIdx) => {
                        const scoreNum =
                          typeof itemScore.score === 'number'
                            ? itemScore.score <= 1
                              ? (itemScore.score * 100).toFixed(0)
                              : itemScore.score.toFixed(1)
                            : '-';
                        const isPass = Boolean(itemScore.passed);
                        const itemId =
                          'item_id' in itemScore
                            ? itemScore.item_id
                            : 'eval_id' in itemScore
                              ? itemScore.eval_id
                              : '';

                        return (
                          <div
                            key={itemId || itemIdx}
                            className="p-2.5 rounded-lg border border-border/60 bg-muted/20 text-xs space-y-1.5"
                          >
                            <div className="flex items-center justify-between">
                              <span className="font-mono text-[10.5px] text-muted-foreground">
                                题目 #{itemIdx + 1} ({itemId?.slice(0, 8)})
                              </span>
                              <div className="flex items-center gap-2 font-mono">
                                <span
                                  className={
                                    isPass
                                      ? 'text-accent-insight font-semibold'
                                      : 'text-destructive font-semibold'
                                  }
                                >
                                  {isPass ? 'PASS' : 'FAIL'} ({scoreNum}%)
                                </span>
                              </div>
                            </div>
                            {itemScore.reason && (
                              <div className="text-[10px] text-muted-foreground">
                                评语: {itemScore.reason}
                              </div>
                            )}
                            {itemScore.metrics && (
                              <div className="pt-1">
                                <RetrievalMetricsView
                                  metrics={itemScore.metrics}
                                  compact
                                  showAuditDetails
                                />
                              </div>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* 多臂检索配置对比弹窗 */}
      {currentSpaceId && isCompareOpen && (
        <Suspense
          fallback={
            <span role="status" className="sr-only">
              正在打开评测对比…
            </span>
          }
        >
          {' '}
          <EvalCompareDialog
            isOpen={isCompareOpen}
            onClose={() => setIsCompareOpen(false)}
            spaceId={currentSpaceId}
            evalItemCount={evalItems.length}
            onComparisonComplete={loadEvalRuns}
          />
        </Suspense>
      )}
    </div>
  );
}
