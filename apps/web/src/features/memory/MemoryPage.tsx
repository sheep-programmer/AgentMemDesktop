import { PageHeader } from '@/components/shared/PageHeader';
import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useSearchParams } from 'react-router';
import type {
  KnowledgeCard,
  Insight,
  ConflictGroup,
  InsightReviewResponse,
} from '@/lib/api/types';
import { documentService } from '@/lib/api/services/documents';
import { memoryService } from '@/lib/api/services/memory';
import { spaceService } from '@/lib/api/services/spaces';
import { useSpaceStore } from '@/stores/useSpaceStore';
import { KnowledgeCardDrawer } from './components/KnowledgeCardDrawer';
import { InsightDetailDrawer } from './components/InsightDetailDrawer';
import { MemoryGraphTab } from './components/MemoryGraphTab';
import { useQueryClient } from '@tanstack/react-query';
import { MemoryCardsTab } from './components/MemoryCardsTab';
import { MemoryInsightsTab } from './components/MemoryInsightsTab';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Layers, Share2, Lightbulb, RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { toast } from 'sonner';
import { useConfirm } from '@/components/shared/ConfirmProvider';

export function MemoryPage() {
  const requestConfirmation = useConfirm();
  const { currentSpaceId } = useSpaceStore();
  const queryClient = useQueryClient();
  // 进化页「候选经验待验证 → 去查看」带着 ?tab=insights&status=candidate 过来：
  // 直接落到候选列表，不让人再自己点一遍标签和筛选
  const [searchParams] = useSearchParams();
  const [activeTab, setActiveTab] = useState<'cards' | 'graph' | 'insights'>(
    () => {
      const tab = searchParams.get('tab');
      return tab === 'insights' || tab === 'graph' ? tab : 'cards';
    },
  );
  const [cards, setCards] = useState<KnowledgeCard[]>([]);
  const [cardsNextCursor, setCardsNextCursor] = useState<string | null>(null);
  // 标签徽章要显示**库里总数**，不是当前这一页的条数：
  // 原本写的是 cards.length，分页 limit=50 时 168 张卡只显示 50
  const [cardsTotal, setCardsTotal] = useState<number | null>(null);
  const [isLoadingMoreCards, setIsLoadingMoreCards] = useState(false);
  const [insights, setInsights] = useState<Insight[]>([]);
  const [insightsNextCursor, setInsightsNextCursor] = useState<string | null>(
    null,
  );
  const [insightsTotal, setInsightsTotal] = useState<number | null>(null);
  const [isLoadingMoreInsights, setIsLoadingMoreInsights] = useState(false);
  const [conflictGroups, setConflictGroups] = useState<ConflictGroup[]>([]);
  const [reviewResponse, setReviewResponse] =
    useState<InsightReviewResponse | null>(null);
  const [activeDrawerCard, setActiveDrawerCard] =
    useState<KnowledgeCard | null>(null);
  const [activeDrawerInsight, setActiveDrawerInsight] =
    useState<Insight | null>(null);
  const [drawerInitialTab, setDrawerInitialTab] = useState<'edit' | 'history'>(
    'edit',
  );
  const [kindFilter, setKindFilter] = useState<string>('all');
  const [statusFilter, setStatusFilter] = useState<string>(() => {
    const status = searchParams.get('status');
    return status && ['active', 'candidate', 'archived'].includes(status)
      ? status
      : 'all';
  });
  const [searchQuery, setSearchQuery] = useState(
    () => searchParams.get('q') || '',
  );
  const [pageStatus, setPageStatus] = useState<
    'loading' | 'empty' | 'error' | 'ready'
  >('loading');
  const [isExtracting, setIsExtracting] = useState(false);
  const [extractDetail, setExtractDetail] = useState<string | null>(null);
  const [canExtract, setCanExtract] = useState(true);
  const [isRefreshing, setIsRefreshing] = useState(false);

  // 搜索与类型筛选走后端：此前只在已加载的 50 张里本地过滤，后面 120 张搜不到，
  // 用户会以为抽取漏了。用 ref 存当前条件，避免 loadMemoryData 随每次输入换身份重拉整页
  const listFilterRef = useRef({
    q: searchQuery.trim(),
    kind: 'all',
    status: statusFilter,
  });
  const cardQuery = () => {
    const { q, kind } = listFilterRef.current;
    return { ...(q ? { q } : {}), ...(kind !== 'all' ? { kind } : {}) };
  };
  const insightQuery = () => {
    const { q, status } = listFilterRef.current;
    return { ...(q ? { q } : {}), ...(status !== 'all' ? { status } : {}) };
  };

  const loadMemoryData = useCallback(async () => {
    if (!currentSpaceId) {
      setPageStatus('empty');
      return false;
    }
    setPageStatus('loading');
    try {
      const [cardsRes, insightsRes, conflictsRes, reviewRes] =
        await Promise.allSettled([
          memoryService.getCards(currentSpaceId, { limit: 50, ...cardQuery() }),
          memoryService.getInsights(currentSpaceId, {
            limit: 50,
            ...insightQuery(),
          }),
          memoryService.getConflicts(currentSpaceId),
          memoryService.getInsightsForReview(currentSpaceId),
        ]);
      if (cardsRes.status === 'fulfilled') {
        setCards(cardsRes.value.items || []);
        setCardsNextCursor(cardsRes.value.next_cursor || null);
        setCardsTotal(cardsRes.value.total ?? null);
      }
      if (insightsRes.status === 'fulfilled') {
        setInsights(insightsRes.value.items || []);
        setInsightsNextCursor(insightsRes.value.next_cursor || null);
        setInsightsTotal(insightsRes.value.total ?? null);
      }
      if (conflictsRes.status === 'fulfilled')
        setConflictGroups(conflictsRes.value || []);
      if (reviewRes.status === 'fulfilled') setReviewResponse(reviewRes.value);

      // `Promise.allSettled` 永远不会 reject——下面那个 catch 其实是死代码，
      // 而这里原本无条件 setPageStatus('ready')：四个请求全挂时页面照样进「就绪」态，
      // 渲染出 0 卡片、0 经验、空图谱，和「这个知识库本来就是空的」完全无法区分。
      // 对一个卖点是记忆沉淀的产品，这会让用户以为数据全没了。
      const results = [cardsRes, insightsRes, conflictsRes, reviewRes];
      const failed = results.filter((r) => r.status === 'rejected');
      if (failed.length === results.length) {
        setPageStatus('error');
        return false;
      }
      setPageStatus('ready');
      if (failed.length > 0) {
        // 部分失败仍然可用（allSettled 就是为此而选），但不能不吭声：
        // 少掉的那部分在界面上同样表现为「空」
        toast.error(
          `记忆数据有 ${failed.length} 项加载失败，页面显示可能不完整`,
        );
      }
      void queryClient.invalidateQueries({
        queryKey: ['knowledge-graph', currentSpaceId],
      });
      return failed.length === 0;
    } catch {
      setPageStatus('error');
      return false;
    }
  }, [currentSpaceId, queryClient]);

  useEffect(() => {
    loadMemoryData();
  }, [loadMemoryData]);

  // 条件变了只重拉两个列表（不动图谱与冲突），输入时防抖
  const isFirstFilterRun = useRef(true);
  useEffect(() => {
    listFilterRef.current = {
      q: searchQuery.trim(),
      kind: kindFilter,
      status: statusFilter,
    };
    if (isFirstFilterRun.current) {
      isFirstFilterRun.current = false;
      return;
    }
    if (!currentSpaceId) return;
    let cancelled = false;
    const timer = window.setTimeout(async () => {
      const [cardsRes, insightsRes] = await Promise.allSettled([
        memoryService.getCards(currentSpaceId, { limit: 50, ...cardQuery() }),
        memoryService.getInsights(currentSpaceId, {
          limit: 50,
          ...insightQuery(),
        }),
      ]);
      if (cancelled) return;
      if (cardsRes.status === 'fulfilled') {
        setCards(cardsRes.value.items || []);
        setCardsNextCursor(cardsRes.value.next_cursor || null);
        setCardsTotal(cardsRes.value.total ?? null);
      }
      if (insightsRes.status === 'fulfilled') {
        setInsights(insightsRes.value.items || []);
        setInsightsNextCursor(insightsRes.value.next_cursor || null);
        setInsightsTotal(insightsRes.value.total ?? null);
      }
      if (cardsRes.status === 'rejected' || insightsRes.status === 'rejected') {
        toast.error('按条件查询失败，列表可能不完整');
      }
    }, 300);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
    // cardQuery / insightQuery 读的是 ref，不必进依赖
  }, [searchQuery, kindFilter, statusFilter, currentSpaceId]);

  // 抽取依赖蒸馏角色：没绑定可用模型时按钮置灰并说明原因，
  // 而不是让用户点了之后收到一个「provider 不可用」
  useEffect(() => {
    let cancelled = false;
    spaceService
      .getSystemCapabilities()
      .then((caps) => {
        if (!cancelled) setCanExtract(Boolean(caps.distill_available));
      })
      .catch(() => {
        if (!cancelled) setCanExtract(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const handleRefresh = async () => {
    setIsRefreshing(true);
    const complete = await loadMemoryData();
    setIsRefreshing(false);
    if (complete) toast.success('记忆数据已刷新');
  };

  const handleLoadMoreCards = async () => {
    if (!currentSpaceId || !cardsNextCursor || isLoadingMoreCards) return;
    setIsLoadingMoreCards(true);
    try {
      const res = await memoryService.getCards(currentSpaceId, {
        limit: 50,
        cursor: cardsNextCursor,
        ...cardQuery(),
      });
      setCards((prev) => [...prev, ...(res.items || [])]);
      setCardsNextCursor(res.next_cursor || null);
    } catch {
      toast.error('加载更多卡片失败');
    } finally {
      setIsLoadingMoreCards(false);
    }
  };

  /** 手动新增的经验直接插到列表最前，不必整页重拉。 */
  const handleInsightCreated = (insight: Insight) => {
    setInsights((prev) => [
      insight,
      ...prev.filter((ins) => ins.id !== insight.id),
    ]);
    setInsightsTotal((prev) => (prev == null ? prev : prev + 1));
    // 新经验是「已生效」：正筛着候选/已归档时它不会出现，刚点完保存却看不到，
    // 会以为没存上。切到能看见它的筛选（切换会触发一次按条件重拉）
    if (statusFilter !== 'all' && statusFilter !== insight.status)
      setStatusFilter('all');
  };

  const handleLoadMoreInsights = async () => {
    if (!currentSpaceId || !insightsNextCursor || isLoadingMoreInsights) return;
    setIsLoadingMoreInsights(true);
    try {
      const res = await memoryService.getInsights(currentSpaceId, {
        limit: 50,
        cursor: insightsNextCursor,
        ...insightQuery(),
      });
      setInsights((prev) => [...prev, ...(res.items || [])]);
      setInsightsNextCursor(res.next_cursor || null);
    } catch {
      toast.error('加载更多经验失败');
    } finally {
      setIsLoadingMoreInsights(false);
    }
  };

  const handleResolveConflict = async (
    groupId: string,
    action: 'keep_a' | 'keep_b' | 'merge',
  ) => {
    if (!currentSpaceId) return;
    const group = conflictGroups.find((g) => g.group_id === groupId);
    const idA = group?.insights?.[0]?.id || 'ins-a';
    const idB = group?.insights?.[1]?.id || 'ins-b';
    const payload =
      action === 'keep_a'
        ? { keep_id: idA, archive_ids: [idB] }
        : action === 'keep_b'
          ? { keep_id: idB, archive_ids: [idA] }
          : {
              keep_id: idA,
              archive_ids: [idB],
              merged_text: `${group?.insights?.[0]?.guidance || ''} ; ${group?.insights?.[1]?.guidance || ''}`,
            };

    try {
      await memoryService.resolveConflict(currentSpaceId, groupId, payload);
      setConflictGroups((prev) => prev.filter((g) => g.group_id !== groupId));
      toast.success('冲突已成功裁决并更新经验条目');
      const updated = await memoryService.getInsights(currentSpaceId);
      setInsights(updated.items || []);
      setInsightsNextCursor(updated.next_cursor || null);
    } catch {
      toast.error('裁决冲突失败');
    }
  };

  const handleSaveCard = async (updated: KnowledgeCard) => {
    try {
      await memoryService.updateCard(currentSpaceId, updated.id, {
        title: updated.title,
        body: updated.body,
        kind: updated.kind,
      });
      setCards((prev) => prev.map((c) => (c.id === updated.id ? updated : c)));
      setActiveDrawerCard(null);
      toast.success('已保存，并标记为人工校验');
    } catch {
      toast.error('保存知识卡片失败');
    }
  };

  const handleDeleteCard = async (card: KnowledgeCard) => {
    if (!currentSpaceId) return;
    try {
      await memoryService.deleteCard(currentSpaceId, card.id);
      setCards((prev) => prev.filter((c) => c.id !== card.id));
      setCardsTotal((prev) => (prev != null ? Math.max(0, prev - 1) : prev));
      setActiveDrawerCard(null);
      toast.success('卡片已删除');
    } catch (err: unknown) {
      toast.error('删除卡片失败', { description: (err as Error)?.message });
    }
  };

  const handleToggleInsightStatus = async (
    insightId: string,
    currentStatus: string,
  ) => {
    const nextStatus = currentStatus === 'active' ? 'archived' : 'active';
    try {
      await memoryService.updateInsight(currentSpaceId, insightId, {
        status: nextStatus as 'active' | 'archived',
      });
      setInsights((prev) =>
        prev.map((ins) =>
          ins.id === insightId
            ? { ...ins, status: nextStatus as 'active' | 'archived' }
            : ins,
        ),
      );
      setActiveDrawerInsight((prev) =>
        prev && prev.id === insightId
          ? { ...prev, status: nextStatus as 'active' | 'archived' }
          : prev,
      );
      toast.success(nextStatus === 'active' ? '经验已激活' : '经验已归档');
    } catch {
      toast.error('更新经验状态失败');
    }
  };

  const handleArchiveInsight = async (insightId: string) => {
    if (!currentSpaceId) return;
    try {
      await memoryService.updateInsight(currentSpaceId, insightId, {
        status: 'archived',
      });
      setInsights((prev) =>
        prev.map((ins) =>
          ins.id === insightId ? { ...ins, status: 'archived' } : ins,
        ),
      );
      setActiveDrawerInsight((prev) =>
        prev && prev.id === insightId ? { ...prev, status: 'archived' } : prev,
      );
      setReviewResponse((prev) => {
        if (!prev) return null;
        return {
          ...prev,
          items: prev.items?.map((item) =>
            item.insight.id === insightId
              ? { ...item, insight: { ...item.insight, status: 'archived' } }
              : item,
          ),
        };
      });
      toast.success('经验已归档');
    } catch {
      toast.error('归档经验失败');
    }
  };

  /** 从已就绪的文档抽取 L2 卡片与实体关系。 */
  const handleExtractCards = useCallback(async () => {
    if (!currentSpaceId || isExtracting) return;

    // 这个按钮是无条件重抽**全部**已就绪文档（`document_ids` 传空即全量），
    // 而摄取时每篇文档已经自动抽过一次。重抽既要为每篇文档各花一次 LLM 调用，
    // 又会为已抽过的内容生成近重复卡片——实测同一篇文档抽两次得到
    // 「KRAS G12C 突变的可成药窗口」与「…的共价可药窗口」这类标题微差、正文相同的卡。
    // 代价不小且不可逆，至少要让用户知道自己按下的是什么。
    let docCount = 0;
    try {
      const res = await documentService.getDocuments(currentSpaceId, {
        limit: 1,
      });
      docCount = res.total ?? 0;
    } catch {
      // 数不出来也不拦着，只是确认文案里不写数字
    }
    const scope =
      docCount > 0 ? `全部 ${docCount} 篇已就绪文档` : '全部已就绪文档';
    const accepted = await requestConfirmation({
      title: '重新抽取知识卡片？',
      description:
        `将对${scope}重新抽取。\n\n` +
        '· 每篇文档各消耗一次模型调用\n' +
        '· 已抽取过的文档会再抽一遍，可能产生重复或标题微差的卡片',
      confirmText: '开始抽取',
    });
    if (!accepted) {
      return;
    }

    setIsExtracting(true);
    setExtractDetail(null);
    try {
      await memoryService.extractCardsStream(
        currentSpaceId,
        {},
        {
          onProgress: (data) =>
            setExtractDetail(
              data.total > 0
                ? `已处理 ${data.done} / ${data.total} 篇文档`
                : '正在抽取…',
            ),
          onError: (data) => toast.error(data.message || '抽取失败'),
          onDone: (data) => {
            setExtractDetail(
              `新增 ${data.cards_created} 张卡片、更新 ${data.cards_updated} 张，` +
                `实体 ${data.entities_created} 个、关系 ${data.relations_created} 条`,
            );
            toast.success('知识卡片抽取完成');
          },
        },
      );
      await loadMemoryData();
    } catch (err: unknown) {
      toast.error(err instanceof Error ? err.message : '抽取失败');
    } finally {
      setIsExtracting(false);
    }
  }, [currentSpaceId, isExtracting, loadMemoryData]);

  const handleCardClick = (card: KnowledgeCard, tab?: 'edit' | 'history') => {
    setActiveDrawerCard(card);
    setDrawerInitialTab(tab || 'edit');
  };

  return (
    <div className="workspace-page">
      <div className="workspace-content space-y-6">
        <PageHeader
          title="把知识连接起来，让经验留下来"
          eyebrow="知识管理 / 记忆"
          icon={Layers}
          description="从资料中提炼知识卡片、探索实体关系，并管理从问答反馈中沉淀的可复用经验。"
        >
          <div className="flex max-w-full flex-wrap items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={handleRefresh}
              disabled={isRefreshing}
              className="h-8 gap-1 text-xs"
            >
              <RefreshCw
                className={`h-3.5 w-3.5 ${isRefreshing ? 'animate-spin' : ''}`}
              />
              刷新
            </Button>
            <Tabs
              value={activeTab}
              onValueChange={(v) =>
                setActiveTab(v as 'cards' | 'graph' | 'insights')
              }
            >
              <TabsList className="bg-muted/50 p-1">
                <TabsTrigger value="cards" className="gap-1.5 text-xs">
                  <Layers className="h-3.5 w-3.5" />
                  卡片 ({cardsTotal ?? cards.length})
                </TabsTrigger>
                <TabsTrigger value="graph" className="gap-1.5 text-xs">
                  <Share2 className="h-3.5 w-3.5" />
                  知识图谱
                </TabsTrigger>
                <TabsTrigger value="insights" className="gap-1.5 text-xs">
                  <Lightbulb className="h-3.5 w-3.5 text-accent-insight" />
                  经验 ({insightsTotal ?? insights.length})
                </TabsTrigger>
              </TabsList>
            </Tabs>
          </div>
        </PageHeader>

        {activeTab === 'cards' && (
          <MemoryCardsTab
            cards={cards}
            pageStatus={pageStatus}
            searchQuery={searchQuery}
            onSearchChange={setSearchQuery}
            kindFilter={kindFilter}
            onKindFilterChange={setKindFilter}
            onCardClick={handleCardClick}
            onRetry={loadMemoryData}
            nextCursor={cardsNextCursor}
            isLoadingMore={isLoadingMoreCards}
            onLoadMore={handleLoadMoreCards}
            onExtract={handleExtractCards}
            isExtracting={isExtracting}
            extractDetail={extractDetail}
            canExtract={canExtract}
          />
        )}

        {activeTab === 'graph' && <MemoryGraphTab spaceId={currentSpaceId} />}

        {activeTab === 'insights' && (
          <MemoryInsightsTab
            insights={insights}
            spaceId={currentSpaceId}
            conflictGroups={conflictGroups}
            reviewItems={reviewResponse?.items || []}
            minApplied={reviewResponse?.min_applied}
            maxSuccessRate={reviewResponse?.max_success_rate}
            pageStatus={pageStatus}
            searchQuery={searchQuery}
            onSearchChange={setSearchQuery}
            statusFilter={statusFilter}
            onStatusFilterChange={setStatusFilter}
            onToggleStatus={handleToggleInsightStatus}
            onInsightClick={setActiveDrawerInsight}
            onArchiveInsight={handleArchiveInsight}
            onResolveConflict={handleResolveConflict}
            onRetry={loadMemoryData}
            nextCursor={insightsNextCursor}
            isLoadingMore={isLoadingMoreInsights}
            onLoadMore={handleLoadMoreInsights}
            onInsightCreated={handleInsightCreated}
          />
        )}

        <KnowledgeCardDrawer
          card={activeDrawerCard}
          spaceId={currentSpaceId}
          isOpen={Boolean(activeDrawerCard)}
          initialTab={drawerInitialTab}
          onClose={() => setActiveDrawerCard(null)}
          onSave={handleSaveCard}
          onDelete={handleDeleteCard}
        />

        <InsightDetailDrawer
          insight={activeDrawerInsight}
          spaceId={currentSpaceId}
          isOpen={Boolean(activeDrawerInsight)}
          onClose={() => setActiveDrawerInsight(null)}
          onStatusChange={(updated) => {
            setInsights((prev) =>
              prev.map((ins) => (ins.id === updated.id ? updated : ins)),
            );
            setActiveDrawerInsight(updated);
          }}
        />
      </div>
    </div>
  );
}
