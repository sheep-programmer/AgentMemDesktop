import { PageHeader } from '@/components/shared/PageHeader';
import React, {
  useState,
  useEffect,
  useCallback,
  useRef,
  lazy,
  Suspense,
} from 'react';
import { useParams, useSearchParams } from 'react-router';
import { useSpaceStore } from '@/stores/useSpaceStore';
import { expertiseService } from '@/lib/api/services/expertise';
import type { components } from '@/lib/api/types.gen';
import type { ConsistencyProbe } from '@/lib/api/types';
import { ExpertiseRadarChart } from './components/ExpertiseRadarChart';
import { ExpertiseScoreOverview } from './components/ExpertiseScoreOverview';
import { ExpertiseGrowthChart } from './components/ExpertiseGrowthChart';
import { KnowledgeGapsTree } from './components/KnowledgeGapsTree';
const EvalSetRunner = lazy(() =>
  import('./components/EvalSetRunner').then((module) => ({
    default: module.EvalSetRunner,
  })),
);
import { ConsistencyProbeResult } from './components/ConsistencyProbeResult';
import { FourStateView } from '@/components/shared/FourStateView';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { FolderTree, CheckSquare, RefreshCw, Sparkles } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { toast } from 'sonner';

type ExpertiseScore = components['schemas']['ExpertiseScore'];
type ExpertiseSnapshot = components['schemas']['ExpertiseSnapshot'];

const emptyExpertise: ExpertiseScore = {
  coverage: 0,
  accuracy: 0,
  consistency: 0,
  groundedness: 0,
  groundedness_samples: 0,
  insight_density: 0,
  overall: 0,
  consistency_source: 'proxy',
  consistency_measured_at: null,
};

export function ExpertisePage() {
  const { currentSpaceId, setCurrentSpaceId } = useSpaceStore();
  const { spaceId: paramSpaceId } = useParams();
  const spaceId = paramSpaceId || currentSpaceId;

  useEffect(() => {
    if (paramSpaceId && paramSpaceId !== currentSpaceId) {
      setCurrentSpaceId(paramSpaceId);
    }
  }, [paramSpaceId, currentSpaceId, setCurrentSpaceId]);

  const [currentExpertise, setCurrentExpertise] =
    useState<ExpertiseScore>(emptyExpertise);
  const [previousExpertise, setPreviousExpertise] =
    useState<ExpertiseSnapshot | null>(null);
  const [history, setHistory] = useState<ExpertiseSnapshot[]>([]);
  const [pageStatus, setPageStatus] = useState<
    'loading' | 'empty' | 'error' | 'ready'
  >('loading');
  // 进化页「先生成测验题」带着 ?tab=evals 过来：直接打开测验题区域并滚过去
  const [searchParams] = useSearchParams();
  const [activeBottomTab, setActiveBottomTab] = useState<'gaps' | 'evals'>(
    () => (searchParams.get('tab') === 'evals' ? 'evals' : 'gaps'),
  );
  const bottomTabsRef = useRef<HTMLDivElement>(null);
  const [isRefreshing, setIsRefreshing] = useState(false);

  // 只在第一次加载完时滚一次：之后点「刷新」重新就绪，不该再把人拽下去
  const scrolledToEvalsRef = useRef(false);
  useEffect(() => {
    if (pageStatus !== 'ready' || scrolledToEvalsRef.current) return;
    if (searchParams.get('tab') !== 'evals') return;
    scrolledToEvalsRef.current = true;
    bottomTabsRef.current?.scrollIntoView({
      behavior: 'smooth',
      block: 'start',
    });
  }, [pageStatus, searchParams]);

  // 一致性实测状态
  const [probeResult, setProbeResult] = useState<ConsistencyProbe | null>(null);
  const [isProbing, setIsProbing] = useState(false);
  const [probeError, setProbeError] = useState<string | null>(null);
  const [showProbeResult, setShowProbeResult] = useState(false);

  const loadData = useCallback(async () => {
    if (!spaceId) {
      setPageStatus('empty');
      return false;
    }
    setPageStatus('loading');
    try {
      const [expRes, histRes] = await Promise.allSettled([
        expertiseService.getExpertise(spaceId),
        expertiseService.getHistory(spaceId),
      ]);
      if (expRes.status === 'fulfilled') setCurrentExpertise(expRes.value);
      if (histRes.status === 'fulfilled') {
        const hist = histRes.value || [];
        setHistory(hist);
        setPreviousExpertise(
          hist.length > 1 ? hist[hist.length - 2] : hist[0] || null,
        );
      } else {
        setHistory([]);
        setPreviousExpertise(null);
      }
      // 当前评分是主数据；请求失败时不能把初始的 0 展示成实际测得值。
      // 历史记录单独失败可保留当前评分，并明确提示该部分不可用。
      if (expRes.status === 'rejected') {
        setPageStatus('error');
        return false;
      }
      setPageStatus('ready');
      if (histRes.status === 'rejected')
        toast.warning('专家度已加载，历史记录暂时无法获取。');
      return histRes.status === 'fulfilled';
    } catch {
      setPageStatus('error');
      return false;
    }
  }, [spaceId]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const handleRefresh = async () => {
    setIsRefreshing(true);
    const complete = await loadData();
    setIsRefreshing(false);
    if (complete) toast.success('专家度度量已重新计算');
  };

  const handleMeasureConsistency = async () => {
    if (!spaceId) return;
    setIsProbing(true);
    setProbeError(null);
    setShowProbeResult(true);
    try {
      const result = await expertiseService.measureConsistency(spaceId, {
        questions: 3,
        repeats: 3,
      });
      setProbeResult(result);
      try {
        const updatedExp = await expertiseService.getExpertise(spaceId);
        setCurrentExpertise(updatedExp);
        toast.success('一致性实测完成，已刷新专家度评分');
      } catch {
        toast.warning('一致性实测已完成，专家度暂时未能刷新，请稍后刷新度量。');
      }
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : '探测回答一致性失败';
      setProbeError(message);
      toast.error(message);
    } finally {
      setIsProbing(false);
    }
  };

  const handleOutlineGenerated = async () => {
    if (!spaceId) return;
    try {
      const [updatedExp, histRes] = await Promise.allSettled([
        expertiseService.getExpertise(spaceId),
        expertiseService.getHistory(spaceId),
      ]);
      if (updatedExp.status === 'fulfilled')
        setCurrentExpertise(updatedExp.value);
      if (histRes.status === 'fulfilled') {
        const hist = histRes.value || [];
        setHistory(hist);
        setPreviousExpertise(
          hist.length > 1 ? hist[hist.length - 2] : hist[0] || null,
        );
      }
    } catch {
      // ignore
    }
  };

  return (
    <div className="workspace-page">
      <div className="workspace-content space-y-6">
        <FourStateView
          status={pageStatus}
          emptyTitle="暂无专家度评测数据"
          emptyDescription="导入资料并完成至少一次问答后，系统将自动计算覆盖率与事实依据度。"
          error="无法加载专家度数据，请检查后端运行状态。"
          onRetry={loadData}
        >
          {/* 顶部标题与操作栏 */}
          <PageHeader
            title="每一步成长，都有据可查"
            eyebrow="能力评测 / 专家度"
            icon={Sparkles}
            description="以覆盖度、准确率、一致性和引用质量衡量专业能力，发现知识缺口，验证每一次进步。"
          >
            <div className="flex items-center gap-2 shrink-0">
              <Button
                variant="outline"
                size="sm"
                onClick={handleMeasureConsistency}
                disabled={isProbing || isRefreshing}
                className="h-8 gap-1.5 text-xs text-primary border-primary/30 hover:bg-primary/10"
              >
                <Sparkles
                  className={`h-3.5 w-3.5 ${isProbing ? 'animate-spin' : ''}`}
                />
                {isProbing ? '测算中...' : '测一次回答一致性'}
              </Button>

              <Button
                variant="outline"
                size="sm"
                onClick={handleRefresh}
                disabled={isRefreshing || isProbing}
                className="h-8 gap-1 text-xs shrink-0"
              >
                <RefreshCw
                  className={`h-3.5 w-3.5 ${isRefreshing ? 'animate-spin' : ''}`}
                />
                刷新度量
              </Button>
            </div>
          </PageHeader>

          {/* 第一行：五维雷达图对比 + 综合得分看板 */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 items-stretch">
            <div className="rounded-2xl border border-border/80 bg-card p-6 shadow-xs flex flex-col justify-between">
              <h3 className="text-sm font-semibold text-foreground mb-2">
                五维雷达基准对比
              </h3>
              <ExpertiseRadarChart
                current={currentExpertise}
                previous={previousExpertise}
              />
            </div>

            <ExpertiseScoreOverview
              current={currentExpertise}
              previous={previousExpertise}
              onMeasureConsistency={handleMeasureConsistency}
              isProbing={isProbing}
              hasProbeResult={Boolean(probeResult)}
              onToggleProbeResult={() => setShowProbeResult((v) => !v)}
            />
          </div>

          {/* 回答一致性实测详情面板 */}
          {(showProbeResult || isProbing || probeError || probeResult) && (
            <ConsistencyProbeResult
              probe={probeResult}
              isLoading={isProbing}
              error={probeError}
              onMeasure={handleMeasureConsistency}
              onClose={() => setShowProbeResult(false)}
            />
          )}

          {/* 第二行：成长曲线面积图 */}
          <div className="rounded-2xl border border-border/80 bg-card p-6 shadow-xs">
            <div className="flex items-center justify-between border-b border-border/40 pb-3">
              <h3 className="text-sm font-semibold text-foreground">
                专家度自进化成长曲线
              </h3>
              <span className="text-xs font-mono text-muted-foreground">
                {history.length > 0
                  ? `近 ${history.length} 次闭环迭代历史`
                  : '暂无闭环迭代历史'}
              </span>
            </div>
            <ExpertiseGrowthChart history={history} />
          </div>

          {/* 第三行：Tab 切换（盲区缺口树 / 黄金评测集） */}
          <div ref={bottomTabsRef} className="space-y-4 scroll-mt-4">
            <Tabs
              value={activeBottomTab}
              onValueChange={(v) => setActiveBottomTab(v as 'gaps' | 'evals')}
            >
              <TabsList className="bg-muted/50 p-1">
                <TabsTrigger value="gaps" className="gap-1.5 text-xs">
                  <FolderTree className="h-3.5 w-3.5" />
                  知识盲区
                </TabsTrigger>
                <TabsTrigger value="evals" className="gap-1.5 text-xs">
                  <CheckSquare className="h-3.5 w-3.5" />
                  测验题
                </TabsTrigger>
              </TabsList>
            </Tabs>

            {activeBottomTab === 'gaps' ? (
              <KnowledgeGapsTree onOutlineGenerated={handleOutlineGenerated} />
            ) : (
              <Suspense
                fallback={
                  <div
                    role="status"
                    className="p-8 text-sm text-muted-foreground"
                  >
                    正在加载测验管理…
                  </div>
                }
              >
                <EvalSetRunner />
              </Suspense>
            )}
          </div>
        </FourStateView>
      </div>
    </div>
  );
}
