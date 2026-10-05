import { PageHeader } from '@/components/shared/PageHeader';
import React, { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router';
import type {
  EvolvePendingSummary,
  EvolveHistoryItem,
} from '@/lib/api/types.temp';
import { evolveService } from '@/lib/api/services/evolve';
import { expertiseService } from '@/lib/api/services/expertise';
import { useSpaceStore } from '@/stores/useSpaceStore';
import { useAsyncTask } from '@/hooks/useAsyncTask';
import { EvolveHeroCard } from './components/EvolveHeroCard';
import {
  EvolvePipelineStages,
  type EvolveStageData,
} from './components/EvolvePipelineStages';
import { EvolveSummaryCard } from './components/EvolveSummaryCard';
import { EvolveHistoryTimeline } from './components/EvolveHistoryTimeline';
import { FourStateView } from '@/components/shared/FourStateView';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { AlertTriangle, ListChecks, Sparkles } from 'lucide-react';
import { toast } from 'sonner';
import { useConfirm } from '@/components/shared/ConfirmProvider';

interface EvolveSummaryData {
  scoreBefore: number;
  scoreAfter: number;
  summary: string;
  promoted: number;
  pendingCandidates: number;
}

export function EvolvePage() {
  const requestConfirmation = useConfirm();
  const navigate = useNavigate();
  const { currentSpaceId } = useSpaceStore();
  const cycleTask = useAsyncTask(currentSpaceId);
  const checkTask = useAsyncTask(currentSpaceId);
  const [taskError, setTaskError] = useState<string | null>(null);
  const [taskNotice, setTaskNotice] = useState<string | null>(null);
  const [pending, setPending] = useState<EvolvePendingSummary>({
    pending_count: 0,
    feedback_count: 0,
    correction_count: 0,
  });
  const [history, setHistory] = useState<EvolveHistoryItem[]>([]);
  const [isEvolving, setIsEvolving] = useState(false);
  const [currentStageIndex, setCurrentStageIndex] = useState(-1);
  // 各阶段从 SSE 收到的真实数据，按阶段名归集
  const [stageData, setStageData] = useState<EvolveStageData>({});
  const [showSummary, setShowSummary] = useState(false);
  const [summaryData, setSummaryData] = useState<EvolveSummaryData | null>(
    null,
  );
  // 点「开始进化」后先查一下测验题数；查的这一小会儿再点不重复触发
  const [isCheckingEvals, setIsCheckingEvals] = useState(false);
  const [noEvalsDialogOpen, setNoEvalsDialogOpen] = useState(false);
  const [pageStatus, setPageStatus] = useState<
    'loading' | 'empty' | 'error' | 'ready'
  >('loading');

  const loadData = useCallback(async () => {
    if (!currentSpaceId) return setPageStatus('empty');
    setPageStatus('loading');
    try {
      const [pendingRes, historyRes] = await Promise.all([
        evolveService.getPending(currentSpaceId),
        evolveService.getHistory(currentSpaceId),
      ]);
      setPending(pendingRes);
      setHistory(historyRes);
      setPageStatus('ready');
    } catch (err: unknown) {
      console.error('Failed to load evolve data:', err);
      setPageStatus('error');
    }
  }, [currentSpaceId]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  /**
   * 点「开始进化」：先看有没有测验题。
   *
   * 没有测验题时闭环不做评测，蒸馏出的经验只会停在「候选」、不会自动生效
   * （见后端 evolve/cycle.py：绝不在没有裁判的情况下把经验转正）。用户点完再去提问，
   * 发现系统「没学会」，会以为功能坏了——所以开跑前就把这件事说清楚，给出两条路。
   */
  const startEvolution = async () => {
    if (!currentSpaceId || cycleTask.running()) return;
    const check = checkTask.start();
    if (!check) return;
    try {
      let evalCount: number | null = null;
      setIsCheckingEvals(true);
      try {
        const res = await expertiseService.getEvals(currentSpaceId, {
          limit: 1,
        });
        // total 缺省时会被补成 0，拿已取到的条数兜一下，别把「有题」误判成「没题」
        if (!check.current()) return;
        evalCount = Math.max(res.total ?? 0, res.items.length);
      } catch {
        // 查不到题数不拦着：照常走下面的确认，进化本身会如实报告跳过了评测
        evalCount = null;
      } finally {
        if (check.current()) setIsCheckingEvals(false);
      }
      if (!check.current()) return;
      if (evalCount === 0) {
        setNoEvalsDialogOpen(true);
        return;
      }
      // 一轮进化要调很多次模型、跑好几分钟：总结反馈、合并去重，再把全部测验题
      // 启用前后各答一遍并打分。点之前说清楚，别让人随手一点就开跑
      const materials = pending.pending_count ?? 0;
      const accepted = await requestConfirmation({
        title: '开始一次进化？',
        description:
          `· 先把 ${materials} 条待学习的反馈总结成候选经验\n` +
          '· 再用全部测验题对比启用前后的回答（每道题都要重新回答并打分）\n\n' +
          '会多次调用模型，通常需要几分钟。停止任务或离开页面会中止请求，已保存的结果会保留。',
        confirmText: '开始进化',
      });
      if (!accepted || !check.current()) {
        return;
      }
      await runEvolution();
    } finally {
      check.finish();
    }
  };

  const runEvolution = async () => {
    if (!currentSpaceId) return;
    const task = cycleTask.start();
    if (!task) return;
    let terminal = false;
    setTaskError(null);
    setTaskNotice(null);
    setIsEvolving(true);
    setShowSummary(false);
    setCurrentStageIndex(0);
    setStageData({}); // 新一轮重新开始累积，别把上一轮的数字留在面板上
    toast.info('开始执行知识闭环进化流程...');

    try {
      await evolveService.runCycle(
        currentSpaceId,
        {
          onStage: (data: {
            stage: string;
            status: string;
            [key: string]: unknown;
          }) => {
            if (!task.current() || terminal) return;
            const { stage: st, ...rest } = data;
            // status 只用于进度，不进面板数字
            const { status: _s, ...detail } = rest;
            void _s;
            // 阶段面板的数字全靠这些 detail 字段。以前这里只取 stage 名去点亮进度，
            // 其余字段直接丢掉，面板于是显示写死的假数据。
            if (Object.keys(detail).length > 0) {
              setStageData((prev) => ({
                ...prev,
                [st]: { ...prev[st], ...detail },
              }));
            }
            // 后端 /evolve/cycle 推的 stage 名是 evaluate（'eval' 只出现在 mock 数据里），
            // 只认 'eval' 的话「评测」这一步在真实运行中永远不会亮
            if (st === 'distill') setCurrentStageIndex(0);
            else if (st === 'merge' || st === 'consolidate')
              setCurrentStageIndex(1);
            else if (st === 'evaluate' || st === 'eval')
              setCurrentStageIndex(2);
            else if (st === 'promote') setCurrentStageIndex(3);
          },
          onDone: (raw: unknown) => {
            if (!task.current() || terminal) return;
            terminal = true;
            const data = raw as
              | {
                  produced?: number;
                  merged?: number;
                  promoted?: number;
                  demoted?: number;
                  eval_delta?: number;
                  delta?: number;
                  expertise_before?: number;
                  expertise_after?: number;
                  pending_candidates?: number;
                }
              | undefined;

            setCurrentStageIndex(4);
            setIsEvolving(false);
            setShowSummary(true);

            // 后端 done 事件里 `delta` 是**专家度**变化、`eval_delta` 才是**评测分**变化，
            // 两者量纲不同（实测同一轮：专家度 +1.3，评测 +7.62）。
            // 此前统一取 `delta` 却把它写成「评测变化」，等于报了个错数。
            const expertiseDelta = data?.delta ?? 0;
            const evalDelta = data?.eval_delta;
            const before = data?.expertise_before ?? 0;
            const after =
              data?.expertise_after ?? +(before + expertiseDelta).toFixed(1);
            const evalText =
              typeof evalDelta === 'number'
                ? `评测变化 ${evalDelta > 0 ? '+' : ''}${evalDelta.toFixed(2)}`
                : '本轮未做评测';
            // 没有被测验题验证的候选不会生效：必须明说，否则用户会以为已经学会了
            const pendingCandidates = data?.pending_candidates ?? 0;
            const pendingText =
              pendingCandidates > 0
                ? `${pendingCandidates} 条候选经验待验证，可在记忆页手动启用。`
                : '';

            setSummaryData({
              scoreBefore: before,
              scoreAfter: after,
              summary: `自动化闭环完成：产出 ${data?.produced || 0} 条经验，合并 ${data?.merged || 0} 条，晋升 ${data?.promoted || 0} 条，${evalText}。${pendingText}`,
              promoted: data?.promoted ?? 0,
              pendingCandidates,
            });
            toast.success('🎉 进化闭环完成！');
            loadData();
          },
          onError: (err: { message: string }) => {
            if (!task.current() || terminal) return;
            terminal = true;
            setCurrentStageIndex(-1);
            setTaskError(err.message || '进化流程执行失败');
            toast.error(err.message || '进化流程执行失败');
            setIsEvolving(false);
          },
        },
        task.signal,
      );
      if (task.current() && !terminal) {
        const reason = '进化连接已结束，未收到完成结果。请刷新记录后确认。';
        setCurrentStageIndex(-1);
        setTaskError(reason);
        toast.error(reason);
      }
    } catch (error: unknown) {
      if (task.current() && !terminal) {
        const reason = error instanceof Error ? error.message : '进化调用失败';
        setCurrentStageIndex(-1);
        setTaskError(reason);
        toast.error(reason);
      }
    } finally {
      if (task.finish()) setIsEvolving(false);
    }
  };

  const stopEvolution = () => {
    cycleTask.cancel();
    checkTask.cancel();
    setIsEvolving(false);
    setCurrentStageIndex(-1);
    setTaskNotice(
      '进化请求已停止，已保存的候选经验和评测结果会保留。可刷新记录确认。',
    );
  };

  return (
    <div className="workspace-page">
      <div className="workspace-content space-y-6">
        <PageHeader
          title="从一次纠正，到持续进步"
          eyebrow="持续学习 / 进化中心"
          icon={Sparkles}
          description="把反馈转化为候选经验，通过对照评测验证效果，让有效的方法逐步沉淀。"
        />
        {taskError && (
          <div
            role="alert"
            className="rounded-xl border border-destructive/30 bg-destructive/5 p-4 text-sm text-destructive"
          >
            {taskError}
            <button
              type="button"
              className="ml-3 underline"
              onClick={() => void loadData()}
            >
              刷新记录
            </button>
          </div>
        )}
        {taskNotice && (
          <div
            role="status"
            className="rounded-xl border border-border bg-muted/30 p-4 text-sm text-muted-foreground"
          >
            {taskNotice}
            <button
              type="button"
              className="ml-3 underline"
              onClick={() => void loadData()}
            >
              刷新记录
            </button>
          </div>
        )}
        <FourStateView
          status={pageStatus}
          emptyTitle="暂无待学习的交互数据"
          emptyDescription="在对话中多提问并给予 👍 / 👎 或人工纠偏，AgentMem 将自动收集经验素材。"
          error="无法加载进化中心数据，请检查后端运行状态。"
          onRetry={loadData}
        >
          <EvolveHeroCard
            pending={pending}
            isEvolving={isEvolving}
            isChecking={isCheckingEvals}
            onStartEvolve={startEvolution}
            onStopEvolve={stopEvolution}
          />

          {(isEvolving || currentStageIndex >= 0) && (
            <EvolvePipelineStages
              currentStageIndex={currentStageIndex}
              stageData={stageData}
            />
          )}

          {showSummary && summaryData && (
            <EvolveSummaryCard
              scoreBefore={summaryData.scoreBefore}
              scoreAfter={summaryData.scoreAfter}
              summary={summaryData.summary}
              promoted={summaryData.promoted}
              pendingCandidates={summaryData.pendingCandidates}
              onReviewCandidates={() =>
                navigate(
                  `/s/${currentSpaceId}/memory?tab=insights&status=candidate`,
                )
              }
            />
          )}

          <EvolveHistoryTimeline history={history} />
        </FourStateView>

        <Dialog open={noEvalsDialogOpen} onOpenChange={setNoEvalsDialogOpen}>
          <DialogContent className="max-w-md">
            <DialogHeader>
              <div className="flex items-center gap-2 text-amber-600 dark:text-amber-400">
                <AlertTriangle className="h-5 w-5" />
                <DialogTitle className="text-base font-semibold">
                  这个空间还没有测验题
                </DialogTitle>
              </div>
              <DialogDescription className="pt-2 text-xs leading-relaxed text-muted-foreground">
                没有测验题，这次总结出的经验只会作为候选，不会自动生效。
              </DialogDescription>
            </DialogHeader>
            <div className="space-y-1.5 rounded-lg border border-border/60 bg-muted/30 p-3 text-xs leading-relaxed text-muted-foreground">
              <p>
                测验题是经验转正的「裁判」：进化时会用它们对比启用经验前后的回答，分数确实提升的经验才会生效。
              </p>
              <p>
                仍然开始的话，候选经验可以之后在记忆页逐条手动启用，或补上测验题后再进化一次。
              </p>
            </div>
            <div className="mt-2 flex items-center justify-end gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  setNoEvalsDialogOpen(false);
                  runEvolution();
                }}
              >
                仍然开始
              </Button>
              <Button
                size="sm"
                className="gap-1.5"
                onClick={() => {
                  setNoEvalsDialogOpen(false);
                  navigate(`/s/${currentSpaceId}/expertise?tab=evals`);
                }}
              >
                <ListChecks className="h-3.5 w-3.5" />
                先生成测验题
              </Button>
            </div>
          </DialogContent>
        </Dialog>
      </div>
    </div>
  );
}
