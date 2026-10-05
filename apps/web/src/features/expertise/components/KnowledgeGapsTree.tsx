import React, { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router';
import { useSpaceStore } from '@/stores/useSpaceStore';
import { expertiseService } from '@/lib/api/services/expertise';
import { spaceService } from '@/lib/api/services/spaces';
import type { KnowledgeGapNode } from '@/lib/api/types';
import { Button } from '@/components/ui/button';
import {
  CheckCircle2,
  AlertCircle,
  PlusCircle,
  FolderTree,
  RefreshCw,
  Loader2,
  Layers,
  Sparkles,
  PencilLine,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { toast } from 'sonner';

interface KnowledgeGapsTreeProps {
  onOutlineGenerated?: () => void;
}

export function KnowledgeGapsTree({ onOutlineGenerated }: KnowledgeGapsTreeProps) {
  const navigate = useNavigate();
  const { currentSpaceId, getCurrentSpace } = useSpaceStore();
  const currentSpace = getCurrentSpace();
  const [gaps, setGaps] = useState<KnowledgeGapNode[]>([]);
  const [outlineSize, setOutlineSize] = useState<number | null>(null);
  const [outlineGeneratedAt, setOutlineGeneratedAt] = useState<number | null>(null);
  /** 模型把领域理解成了什么：空间名是简称时，理解偏了整棵盲区树都是错的 */
  const [interpretation, setInterpretation] = useState<string | null>(null);
  /** 还没有大纲、且最近一次自动生成失败的原因（后端冷却期内不再自动重试） */
  const [outlineError, setOutlineError] = useState<string | null>(null);
  // 就地改领域：理解偏了不必再绕去空间切换弹窗，改完直接重新生成
  const [isEditingDomain, setIsEditingDomain] = useState(false);
  const [domainDraft, setDomainDraft] = useState('');
  const [outlineStats, setOutlineStats] = useState<{ covered: number; total: number } | null>(null);
  const [isLoadingGaps, setIsLoadingGaps] = useState(false);
  const [isGeneratingOutline, setIsGeneratingOutline] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const loadGaps = useCallback(async () => {
    if (!currentSpaceId) return;
    setIsLoadingGaps(true);
    setErrorMsg(null);
    try {
      const data = await expertiseService.getGaps(currentSpaceId);
      setGaps(data.gaps || []);
      setOutlineSize(data.outline_size ?? null);
      setOutlineGeneratedAt(data.outline_generated_at ?? null);
      setInterpretation(data.outline_interpretation ?? null);
      setOutlineError(data.outline_error ?? null);
    } catch (err: unknown) {
      console.error('Failed to load knowledge gaps:', err);
      setErrorMsg('获取知识盲区数据失败');
    } finally {
      setIsLoadingGaps(false);
    }
  }, [currentSpaceId]);

  useEffect(() => {
    loadGaps();
  }, [loadGaps]);

  const handleRefreshOutline = async () => {
    if (!currentSpaceId || isGeneratingOutline) return;
    setIsGeneratingOutline(true);
    setErrorMsg(null);

    try {
      const res = await expertiseService.generateOutline(currentSpaceId);
      setOutlineStats({ covered: res.covered, total: res.total });
      setOutlineSize(res.total);
      setOutlineGeneratedAt(Date.now());

      // 成功后重新拉取盲区树，并通知父组件刷新专家度得分
      await loadGaps();
      onOutlineGenerated?.();

      const pct = res.total > 0 ? Math.round((res.covered / res.total) * 100) : 0;
      // 领域名常是简称：把模型的理解亮出来，理解偏了用户才知道要去改领域描述
      toast.success(`领域大纲已更新：覆盖 ${res.covered} / ${res.total} 个主题 (${pct}%)`, {
        description: res.interpretation
          ? `按「${res.interpretation}」理解生成。理解不对的话，可在左上角的空间切换里编辑领域后重新生成。`
          : undefined,
        duration: res.interpretation ? 8000 : undefined,
      });
    } catch (err: unknown) {
      // 后端的错误信息本身就是一句完整的话（含模型原话与处理建议），不再叠一层前缀
      const msg = (err as Error)?.message || '生成领域大纲失败，请稍后重试';
      setErrorMsg(msg);
      toast.error('领域大纲生成失败', { description: msg, duration: 10000 });
    } finally {
      setIsGeneratingOutline(false);
    }
  };

  const startEditDomain = () => {
    setDomainDraft(currentSpace?.domain ?? '');
    setIsEditingDomain(true);
  };

  /** 保存新领域（后端会同步进提示词用的 persona），然后按新领域重新生成大纲。 */
  const handleSaveDomain = async (e: React.FormEvent) => {
    e.preventDefault();
    const domain = domainDraft.trim();
    if (!currentSpaceId || !domain) return;
    try {
      const updated = await spaceService.updateSpace(currentSpaceId, { domain });
      const { spaces, setSpaces } = useSpaceStore.getState();
      setSpaces(spaces.map((item) => (item.id === currentSpaceId ? { ...item, ...updated } : item)));
      setIsEditingDomain(false);
    } catch (err: unknown) {
      toast.error('领域保存失败', { description: (err as Error)?.message });
      return;
    }
    await handleRefreshOutline();
  };

  const domainEditor = isEditingDomain ? (
    <form onSubmit={handleSaveDomain} className="flex flex-wrap items-center gap-2">
      <input
        autoFocus
        value={domainDraft}
        onChange={(e) => setDomainDraft(e.target.value)}
        placeholder="把领域写具体，例如：长江大学教务与校园事务"
        className="min-w-0 flex-1 rounded-md border border-input bg-background px-2.5 py-1.5 text-xs text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
      />
      <Button type="submit" size="sm" className="h-7 text-xs" disabled={!domainDraft.trim() || isGeneratingOutline}>
        保存并重新生成
      </Button>
      <Button type="button" size="sm" variant="ghost" className="h-7 text-xs" onClick={() => setIsEditingDomain(false)}>
        取消
      </Button>
    </form>
  ) : null;

  const isOutlineMissing =
    (outlineSize === null || outlineSize === 0) || outlineGeneratedAt === null;

  const renderNode = (node: KnowledgeGapNode, depth = 0): React.ReactNode => {
    const isUngrounded = !node.covered;

    return (
      <div key={node.id} className="space-y-1.5">
        <div
          style={{ paddingLeft: `${depth * 20 + 8}px` }}
          className={cn(
            'flex items-center justify-between rounded-lg border p-2.5 text-xs transition-colors',
            isUngrounded
              ? 'border-destructive/40 bg-destructive/5 text-foreground'
              : 'border-border/60 bg-card hover:bg-muted/40 text-foreground',
          )}
        >
          <div className="flex items-center gap-2">
            {/* 描边按钮：红底是「删除」的视觉语言，一页十几个会让人以为点了会删东西 */}
            {isUngrounded ? (
              <AlertCircle className="h-4 w-4 text-destructive shrink-0" />
            ) : (
              <CheckCircle2 className="h-4 w-4 text-accent-insight shrink-0" />
            )}
            <span className={cn('font-medium', isUngrounded && 'text-destructive font-semibold')}>
              {node.title || node.topic}
            </span>
            {node.reason && (
              <span className="text-[11px] text-muted-foreground hidden sm:inline-block">
                — {node.reason}
              </span>
            )}
          </div>

          <div className="flex items-center gap-2">
            {isUngrounded ? (
              <Button
                size="sm"
                variant="outline"
                onClick={() => navigate(`/s/${currentSpaceId}/library?import=1`)}
                className="h-6 gap-1 px-2 text-[11px] font-normal"
                title={`为「${node.title || node.topic}」导入资料`}
              >
                <PlusCircle className="h-3 w-3" />
                补充资料
              </Button>
            ) : (
              <span className="font-mono text-[10px] text-muted-foreground">
                {node.doc_count || 0} 篇证据支撑
              </span>
            )}
          </div>
        </div>

        {node.children?.map((child) => renderNode(child, depth + 1))}
      </div>
    );
  };

  return (
    <div className="rounded-2xl border border-border bg-card p-6 shadow-2xs space-y-4">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <FolderTree className="h-4 w-4 text-primary" />
          <h3 className="text-sm font-semibold text-foreground">
            知识盲区
          </h3>
          {outlineStats && (
            <span className="rounded-md bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-primary flex items-center gap-1">
              <Layers className="h-3 w-3" />
              大纲覆盖: {outlineStats.covered} / {outlineStats.total} (
              {outlineStats.total > 0
                ? Math.round((outlineStats.covered / outlineStats.total) * 100)
                : 0}
              %)
            </span>
          )}
        </div>

        <Button
          variant="outline"
          size="sm"
          onClick={handleRefreshOutline}
          disabled={isGeneratingOutline || isLoadingGaps}
          className="h-7 text-xs gap-1.5 border-primary/30 text-primary hover:bg-primary/5 cursor-pointer"
        >
          {isGeneratingOutline ? (
            <>
              <Loader2 className="h-3 w-3 animate-spin text-primary" />
              <span>推演领域大纲中...</span>
            </>
          ) : (
            <>
              <RefreshCw className="h-3 w-3" />
              <span>刷新盲区 (生成大纲)</span>
            </>
          )}
        </Button>
      </div>

      <p className="text-xs text-muted-foreground leading-relaxed">
        红框节点表示当前尚未被有效知识卡片或文档切片充分覆盖的薄弱盲区。点击「刷新盲区」将调用大模型推演该领域的全局知识大纲并计算覆盖率。
      </p>

      {interpretation && !isGeneratingOutline && (
        <div className="space-y-2 rounded-lg border border-border/70 bg-muted/30 px-3 py-2 text-xs leading-relaxed">
          <div className="flex flex-wrap items-center gap-x-1 gap-y-1">
            <span className="text-muted-foreground">大纲按这个理解生成：</span>
            <span className="font-medium text-foreground">{interpretation}</span>
            {!isEditingDomain && (
              <button
                type="button"
                onClick={startEditDomain}
                className="ml-1 inline-flex items-center gap-1 font-medium text-primary hover:underline cursor-pointer"
              >
                <PencilLine className="h-3 w-3" />
                理解不对？修改领域
              </button>
            )}
          </div>
          {domainEditor}
        </div>
      )}

      {/* 大模型长耗时调用提示 */}
      {isGeneratingOutline && (
        <div className="rounded-xl border border-primary/30 bg-primary/5 p-3.5 flex items-center gap-3 text-xs text-primary animate-pulse">
          <Sparkles className="h-4 w-4 shrink-0" />
          <span>
            大模型正在深度解析知识库、梳理目标领域大纲并推演盲区，约需 5~15 秒，请稍候...
          </span>
        </div>
      )}

      {/* 失败重试提示 */}
      {errorMsg && !isGeneratingOutline && (
        <div className="rounded-lg bg-destructive/10 border border-destructive/30 p-3 flex items-center justify-between gap-2 text-xs text-destructive">
          <div className="flex items-center gap-2">
            <AlertCircle className="h-4 w-4 shrink-0" />
            <span>{errorMsg}</span>
          </div>
          <Button
            size="sm"
            variant="ghost"
            onClick={handleRefreshOutline}
            className="h-6 px-2 text-[11px] text-destructive hover:bg-destructive/20"
          >
            重试生成
          </Button>
        </div>
      )}

      <div className="space-y-2 pt-1">
        {isLoadingGaps && !isGeneratingOutline ? (
          <div className="py-8 text-center text-xs text-muted-foreground flex items-center justify-center gap-2">
            <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
            <span>正在加载知识盲区...</span>
          </div>
        ) : isOutlineMissing && gaps.length === 0 ? (
          /* 修复遗留空态：大纲未生成时明确提示，绝不误导成「没有盲区」 */
          <div className="py-10 px-4 text-center space-y-3 rounded-xl border border-dashed border-border/80 bg-muted/20">
            <FolderTree className="h-8 w-8 text-muted-foreground mx-auto" />
            <div className="space-y-1">
              <div className="text-sm font-semibold text-foreground">领域大纲尚未生成</div>
              <p className="text-xs text-muted-foreground max-w-md mx-auto">
                当前知识空间尚未构建领域大纲，无法准确测算知识盲区与覆盖度。请点击「生成领域大纲」，由大模型梳理知识拓扑。
              </p>
              {outlineError && (
                <p className="text-xs text-destructive max-w-md mx-auto pt-1">上次自动生成失败：{outlineError}</p>
              )}
            </div>
            {isEditingDomain ? (
              <div className="max-w-md mx-auto text-left">{domainEditor}</div>
            ) : (
              <button
                type="button"
                onClick={startEditDomain}
                className="inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline cursor-pointer"
              >
                <PencilLine className="h-3 w-3" />
                当前领域：{currentSpace?.domain || '未设置'}，修改
              </button>
            )}
            <Button
              size="sm"
              onClick={handleRefreshOutline}
              disabled={isGeneratingOutline}
              className="gap-1.5 text-xs h-8 shadow-xs"
            >
              <RefreshCw
                className={`h-3.5 w-3.5 ${isGeneratingOutline ? 'animate-spin' : ''}`}
              />
              {isGeneratingOutline ? '大模型正在生成大纲...' : '生成领域大纲'}
            </Button>
          </div>
        ) : gaps.length === 0 ? (
          <div className="py-8 text-center text-xs text-muted-foreground">
            当前知识库覆盖完备，未发现明显薄弱盲区。
          </div>
        ) : (
          gaps.map((rootNode) => renderNode(rootNode, 0))
        )}
      </div>
    </div>
  );
}
