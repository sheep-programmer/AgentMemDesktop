import { PageHeader } from '@/components/shared/PageHeader';
import { formatShortcut } from '@/lib/platform';
import React, { useState, useEffect, useRef } from 'react';
import { useParams, useSearchParams } from 'react-router';
import { useSpaceStore } from '@/stores/useSpaceStore';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  usePaginatedSearch,
  type ListOptions,
} from '@/hooks/usePaginatedSearch';
import type { PageResponse } from '@/lib/api/types.temp';
import { documentService } from '@/lib/api';
import type { DocumentItem } from '@/lib/api/types.temp';
import { DocumentCard } from './components/DocumentCard';
import { DocumentReaderModal } from './components/DocumentReaderModal';
import { DocumentUploadModal } from './components/DocumentUploadModal';
import { reportUploadResult } from './lib/uploadToast';
import { IngestionQueueBar } from './components/IngestionQueueBar';
import { RetryFailedDocumentsBar } from './components/RetryFailedDocumentsBar';
import { FourStateView } from '@/components/shared/FourStateView';
import { StatusBadge } from '@/components/shared/StatusBadge';
import {
  Upload,
  Search,
  LayoutGrid,
  List,
  Trash2,
  ExternalLink,
  RotateCw,
  FileText,
  BookOpen,
  Loader2,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { toast } from 'sonner';
import { useConfirm } from '@/components/shared/ConfirmProvider';

const fetchDocumentPage = (spaceId: string, options: ListOptions) =>
  documentService.getDocuments(spaceId, options);

export function LibraryPage() {
  const { spaceId: paramSpaceId } = useParams();
  const { currentSpaceId } = useSpaceStore();
  const spaceId = paramSpaceId || currentSpaceId;

  //: 每篇文档当前的摄取阶段与百分比，来自摄取事件流
  const [ingestProgress, setIngestProgress] = useState<
    Record<string, { stage: string; percent: number }>
  >({});
  const queryClient = useQueryClient();
  const deletionInFlight = useRef(false);
  const [isDeleting, setIsDeleting] = useState(false);
  const requestConfirmation = useConfirm();
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [searchFilter, setSearchFilter] = useState('');
  const [viewMode, setViewMode] = useState<'grid' | 'table'>('grid');
  const [activeDoc, setActiveDoc] = useState<DocumentItem | null>(null);
  const [isUploadOpen, setIsUploadOpen] = useState(false);
  // 从对话页、命令面板、⌘U 过来时带着 ?import=1：直接打开导入弹窗，不让人再找一次按钮
  const [searchParams, setSearchParams] = useSearchParams();
  useEffect(() => {
    if (searchParams.get('import') !== '1') return;
    setIsUploadOpen(true);
    const next = new URLSearchParams(searchParams);
    next.delete('import');
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams]);
  const [isDragOver, setIsDragOver] = useState(false);
  const list = usePaginatedSearch({
    key: 'documents',
    spaceId,
    search: searchFilter,
    fetchPage: fetchDocumentPage,
  });
  const documents = list.items;
  const pageStatus = list.status;
  const isLoadingMore = list.isLoadingMore;
  const countKey = ['document-count', spaceId];
  const documentCount = useQuery({
    queryKey: countKey,
    enabled: Boolean(spaceId),
    queryFn: ({ signal }) =>
      documentService.getDocuments(spaceId, { limit: 1, signal }),
    retry: false,
    staleTime: 30_000,
  });
  const totalDocs =
    documentCount.data?.total ??
    (!list.query && list.status === 'ready' ? list.total : null);
  const loadDocuments = async () => {
    if (!spaceId) return false;
    const [loaded, count] = await Promise.all([
      list.refresh(),
      documentCount.refetch(),
    ]);
    return loaded && !count.isError;
  };

  useEffect(() => {
    setSelectedIds(new Set());
  }, [searchFilter]);

  const removeDeletedDocuments = async (ids: string[]) => {
    queryClient.setQueryData<PageResponse<DocumentItem>>(
      countKey,
      (previous) =>
        previous && {
          ...previous,
          total: Math.max(0, previous.total - ids.length),
        },
    );
    await Promise.all([
      list.removeItems(ids),
      queryClient.invalidateQueries({ queryKey: countKey }),
    ]);
  };

  const handleLoadMore = async () => {
    try {
      await list.loadMore();
    } catch (error: unknown) {
      toast.error(`加载更多失败：${(error as Error).message || '网络错误'}`);
    }
  };

  const handleReprocess = async (docId: string) => {
    try {
      await documentService.reprocessDocument(docId, spaceId);
      toast.success('已触发重新解析');
      loadDocuments();
    } catch {
      toast.error('重新解析失败');
    }
  };

  const handleDelete = async (docId: string) => {
    if (deletionInFlight.current) return;
    // Lock before awaiting confirmation: two rapid clicks must not open two confirmations.
    deletionInFlight.current = true;
    const accepted = await requestConfirmation({
      title: '删除这份资料？',
      description: '关联的切片、检索索引和知识关联也会被移除。这个操作无法撤销。',
      confirmText: '删除资料',
      destructive: true,
    });
    if (!accepted) {
      deletionInFlight.current = false;
      return;
    }
    setIsDeleting(true);
    try {
      await documentService.deleteDocument(docId, spaceId);
      setSelectedIds((previous) => {
        const next = new Set(previous);
        next.delete(docId);
        return next;
      });
      await removeDeletedDocuments([docId]);
      toast.success('文献已删除');
    } catch {
      toast.error('删除文献失败');
    } finally {
      deletionInFlight.current = false;
      setIsDeleting(false);
    }
  };

  const toggleSelect = (id: string) => {
    if (deletionInFlight.current) return;
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleSelectAll = () => {
    if (deletionInFlight.current) return;
    setSelectedIds((previous) => {
      const next = new Set(previous);
      const allSelected = filteredDocs.every((doc) => previous.has(doc.id));
      for (const doc of filteredDocs) {
        if (allSelected) next.delete(doc.id);
        else next.add(doc.id);
      }
      return next;
    });
  };

  const filteredDocs = documents;

  const failedDocs = documents.filter((doc) => doc.status === 'failed');

  const handleBatchDelete = async () => {
    if (deletionInFlight.current || selectedIds.size === 0) return;
    const snapshot = [...selectedIds];
    // Lock before awaiting confirmation so a double click cannot queue two batches.
    deletionInFlight.current = true;
    const accepted = await requestConfirmation({
      title: `删除选中的 ${snapshot.length} 篇资料？`,
      description: '每份资料的切片、检索索引和知识关联都会被移除。失败的资料会继续保留在列表中。',
      confirmText: '删除资料',
      destructive: true,
    });
    if (!accepted) {
      deletionInFlight.current = false;
      return;
    }
    setIsDeleting(true);
    const deleted: string[] = [];
    const failed: string[] = [];
    try {
      for (const id of snapshot) {
        try {
          await documentService.deleteDocument(id, spaceId);
          deleted.push(id);
        } catch {
          failed.push(id);
        }
      }
      setSelectedIds(new Set(failed));
      if (deleted.length > 0) await removeDeletedDocuments(deleted);
      if (failed.length === 0) toast.success(`已删除 ${deleted.length} 篇文献`);
      else if (deleted.length === 0)
        toast.error(`${failed.length} 篇文献删除失败，均未删除`);
      else
        toast.warning(
          `已删除 ${deleted.length} 篇，${failed.length} 篇失败（仍保留在列表中）`,
        );
    } finally {
      deletionInFlight.current = false;
      setIsDeleting(false);
    }
  };

  // 全局整页拖入投喂
  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragOver(true);
  };

  const handleDragLeave = (e: React.DragEvent) => {
    e.preventDefault();
    // 仅在离开最外层页面容器时还原
    if (e.currentTarget.contains(e.relatedTarget as Node)) return;
    setIsDragOver(false);
  };

  const handleDrop = async (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragOver(false);
    const files = Array.from(e.dataTransfer.files);
    if (files.length === 0 || !spaceId) return;

    try {
      reportUploadResult(await documentService.uploadFiles(spaceId, files));
    } catch (err: unknown) {
      toast.error(`导入失败：${(err as Error)?.message || '未知错误'}`);
    } finally {
      loadDocuments();
    }
  };

  // 状态文案与配色统一交给 StatusBadge：它在 Memory 页也在用，
  // 各页各写一套的话，同一个状态在不同页面会有不同说法
  const renderStatusBadge = (status: string) => (
    <StatusBadge status={status === 'active' ? 'ready' : status} />
  );

  return (
    <div
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
      className="workspace-page relative"
    >
      {/* 拖拽投喂指示蒙层 */}
      {isDragOver && (
        <div className="absolute inset-0 z-50 flex flex-col items-center justify-center bg-primary/10 backdrop-blur-xs border-2 border-dashed border-primary m-4 rounded-2xl transition-all pointer-events-none">
          <Upload className="h-12 w-12 text-primary animate-bounce mb-2" />
          <span className="text-base font-semibold text-primary">
            松开即可导入到当前知识空间
          </span>
          <span className="text-xs text-muted-foreground mt-1">
            支持 PDF, Markdown, TXT, DOCX 等文献资料
          </span>
        </div>
      )}

      <div className="workspace-content space-y-6">
        <PageHeader
          title="让每一份资料，成为知识的起点"
          eyebrow="知识管理 / 资料库"
          icon={FileText}
          description="集中管理文档、笔记与网页。导入后自动建立检索索引，回答中的每一条引用都可以追溯。"
        >
          <Button
            variant="outline"
            disabled={isRefreshing || isDeleting}
            aria-label="刷新资料"
            onClick={async () => {
              setIsRefreshing(true);
              const complete = await loadDocuments();
              setIsRefreshing(false);
              if (complete) toast.success('资料列表已刷新');
              else toast.error('资料刷新未完成，请检查服务状态。');
            }}
            className="gap-1.5"
          >
            <RotateCw className={cn('h-4 w-4', isRefreshing && 'animate-spin')} />
            {isRefreshing ? '刷新中…' : '刷新'}
          </Button>
          <Button
            onClick={() => setIsUploadOpen(true)}
            className="gap-1.5 shadow-xs shrink-0"
            disabled={!spaceId}
          >
            <Upload className="h-4 w-4" />
            <span>导入资料</span>
            <span className="text-[10px] opacity-70">{formatShortcut('⌘U')}</span>
          </Button>
        </PageHeader>

        {pageStatus !== 'loading' && pageStatus !== 'error' && (
          <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
            {[
              {
                label: '资料总数',
                value: totalDocs ?? '—',
                note: '当前空间的全部资料',
                icon: FileText,
                color: 'text-primary',
              },
              {
                label: '可以检索',
                value: documents.filter((d) => d.status === 'ready').length,
                note: '当前列表中的就绪资料',
                icon: BookOpen,
                color: 'text-accent-insight',
              },
              {
                label: '正在处理',
                value: documents.filter(
                  (d) => !['ready', 'failed'].includes(d.status),
                ).length,
                note: '当前列表中的处理任务',
                icon: Loader2,
                color: 'text-accent-ai',
              },
              {
                label: '需要关注',
                value: failedDocs.length,
                note: '当前列表中的失败任务',
                icon: RotateCw,
                color: 'text-accent-warn',
              },
            ].map(({ label, value, note, icon: Icon, color }) => (
              <div
                key={label}
                className="rounded-2xl border border-border/80 bg-card p-4 shadow-2xs sm:p-5"
              >
                <div className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
                  <span>{label}</span>
                  <Icon className={cn('h-4 w-4', color)} />
                </div>
                <div className="mt-3 font-mono text-3xl font-semibold tabular-nums tracking-tight text-foreground">
                  {value}
                </div>
                <p className="mt-2 text-[11px] leading-relaxed text-muted-foreground">
                  {note}
                </p>
              </div>
            ))}
          </div>
        )}

        <IngestionQueueBar
          spaceId={spaceId}
          documents={documents}
          onDocumentUpdated={(docId, status) => {
            const document = documents.find((item) => item.id === docId);
            if (document) list.updateItem({ ...document, status });
          }}
          onProgress={(docId, progress) =>
            setIngestProgress((prev) => ({ ...prev, [docId]: progress }))
          }
        />

        <RetryFailedDocumentsBar
          spaceId={spaceId}
          failedDocuments={failedDocs}
          onRetryComplete={loadDocuments}
        />

        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="relative min-w-0 flex-1 basis-[180px] max-w-sm">
            <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
            <input
              type="text"
              disabled={isDeleting}
              maxLength={200}
              aria-label="搜索资料标题或来源"
              placeholder="搜索全部资料的标题或来源..."
              value={searchFilter}
              onChange={(e) => setSearchFilter(e.target.value)}
              className="w-full rounded-lg border border-border/80 bg-card/60 py-1.5 pl-9 pr-3 text-xs text-foreground placeholder:text-muted-foreground outline-none focus:border-primary/50 focus:ring-1 focus:ring-primary/20 transition-all"
            />
          </div>

          <div className="flex items-center gap-2">
            {selectedIds.size > 0 && (
              <div className="flex items-center gap-2 rounded-lg border border-border bg-muted/40 px-3 py-1 text-xs">
                <span>已选 {selectedIds.size} 项</span>
                <button
                  type="button"
                  onClick={handleBatchDelete}
                  disabled={isDeleting}
                  className="flex items-center gap-1 text-destructive hover:underline ml-2"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                  <span>{isDeleting ? '删除中…' : '删除'}</span>
                </button>
              </div>
            )}

            <div className="flex items-center rounded-lg border border-border/80 bg-card/60 p-0.5">
              <button
                type="button"
                onClick={() => setViewMode('grid')}
                aria-label="卡片视图"
                aria-pressed={viewMode === 'grid'}
                className={cn(
                  'flex h-9 w-9 items-center justify-center rounded-md text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary',
                  viewMode === 'grid'
                    ? 'bg-primary text-primary-foreground shadow-xs'
                    : 'text-muted-foreground hover:text-foreground',
                )}
                title="网格视图"
              >
                <LayoutGrid className="h-4 w-4" />
              </button>
              <button
                type="button"
                onClick={() => setViewMode('table')}
                aria-label="列表视图"
                aria-pressed={viewMode === 'table'}
                className={cn(
                  'flex h-9 w-9 items-center justify-center rounded-md text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary',
                  viewMode === 'table'
                    ? 'bg-primary text-primary-foreground shadow-xs'
                    : 'text-muted-foreground hover:text-foreground',
                )}
                title="列表表格视图"
              >
                <List className="h-4 w-4" />
              </button>
            </div>
          </div>
        </div>

        {list.error && list.items.length > 0 && (
          <p role="alert" className="text-sm text-destructive">
            刷新失败，当前显示上次成功加载的资料。
            <button
              className="ml-2 underline"
              onClick={() => void loadDocuments()}
            >
              重试
            </button>
          </p>
        )}
        <FourStateView
          status={pageStatus}
          emptyTitle={
            searchFilter.trim() ? '没有匹配的资料' : '这个知识空间还没有资料'
          }
          emptyDescription={
            searchFilter.trim()
              ? '搜索已覆盖当前空间的全部资料。换个关键词，或清除筛选查看全部资料。'
              : '把这个领域的文档、笔记或网页拖到这里，系统会自动切片并构建知识图谱。'
          }
          emptyActionLabel={searchFilter.trim() ? '清除筛选' : '导入第一份资料'}
          onEmptyAction={() =>
            searchFilter.trim() ? setSearchFilter('') : setIsUploadOpen(true)
          }
          error="后端接口连接失败或空间不存在。"
          onRetry={loadDocuments}
        >
          {filteredDocs.length === 0 && documents.length > 0 ? (
            <div
              role="status"
              className="flex min-h-64 flex-col items-center justify-center rounded-2xl border border-dashed border-border bg-card/50 p-6 text-center"
            >
              <Search className="mb-3 h-7 w-7 text-muted-foreground" />
              <h2 className="text-base font-semibold text-foreground">
                没有匹配的资料
              </h2>
              <p className="mt-2 text-sm text-muted-foreground">
                换个关键词，或清除筛选查看全部资料。
              </p>
              <Button
                variant="outline"
                className="mt-4"
                onClick={() => setSearchFilter('')}
              >
                清除筛选
              </Button>
            </div>
          ) : viewMode === 'grid' ? (
            /* 网格视图 */
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4.5 pt-1 items-stretch">
              {filteredDocs.map((doc) => (
                <DocumentCard
                  progress={ingestProgress[doc.id]}
                  key={doc.id}
                  document={doc}
                  isSelected={selectedIds.has(doc.id)}
                  disabled={isDeleting}
                  onToggleSelect={toggleSelect}
                  onReprocess={handleReprocess}
                  onDelete={handleDelete}
                  onClick={() => setActiveDoc(doc)}
                />
              ))}
            </div>
          ) : (
            /* 列表表格视图 (对标 Linear / Dify 桌面数据表格) */
            <div className="rounded-xl border border-border/80 bg-card overflow-x-auto shadow-xs">
              <table className="w-full min-w-[680px] text-left text-xs">
                <thead>
                  <tr className="border-b border-border/60 bg-muted/30 text-muted-foreground">
                    <th className="w-10 px-3 py-2.5">
                      <input
                        type="checkbox"
                        aria-label="选择当前已加载的全部资料"
                        ref={(node) => { if (node) node.indeterminate = filteredDocs.some((doc) => selectedIds.has(doc.id)) && !filteredDocs.every((doc) => selectedIds.has(doc.id)); }}
                        checked={
                          filteredDocs.length > 0 &&
                          filteredDocs.every((doc) => selectedIds.has(doc.id))
                        }
                        disabled={isDeleting}
                        onChange={toggleSelectAll}
                        className="rounded border-border"
                      />
                    </th>
                    <th className="px-3 py-2.5 font-medium">文献名称</th>
                    <th className="px-3 py-2.5 font-medium">体量</th>
                    <th className="px-3 py-2.5 font-medium">处理状态</th>
                    <th className="px-3 py-2.5 font-medium">更新时间</th>
                    <th className="px-3 py-2.5 text-right font-medium pr-4">
                      操作
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border/40">
                  {filteredDocs.map((doc) => {
                    const isSelected = selectedIds.has(doc.id);
                    return (
                      <tr
                        key={doc.id}
                        className={cn(
                          'group transition-colors hover:bg-muted/30',
                          isSelected && 'bg-primary/5',
                        )}
                      >
                        <td className="px-3 py-3">
                          <input
                            type="checkbox"
                            aria-label={`选择资料：${doc.title}`}
                            checked={isSelected}
                            disabled={isDeleting}
                            onChange={() => toggleSelect(doc.id)}
                            className="rounded border-border"
                          />
                        </td>
                        <td className="px-3 py-3">
                          <div
                            onClick={() => setActiveDoc(doc)}
                            className="flex items-center gap-2 cursor-pointer"
                          >
                            <FileText className="h-4 w-4 text-primary shrink-0" />
                            <span className="font-medium text-foreground hover:underline line-clamp-1 max-w-md">
                              {doc.title}
                            </span>
                          </div>
                        </td>
                        <td className="px-3 py-3 font-mono text-[11px] text-muted-foreground whitespace-nowrap">
                          {doc.token_count.toLocaleString()} tok
                        </td>
                        <td className="px-3 py-3 whitespace-nowrap">
                          {renderStatusBadge(doc.status)}
                        </td>
                        <td className="px-3 py-3 text-muted-foreground whitespace-nowrap text-[11px]">
                          {new Date(doc.updated_at).toLocaleDateString(
                            'zh-CN',
                            {
                              month: 'numeric',
                              day: 'numeric',
                            },
                          )}
                        </td>
                        <td className="px-3 py-3 text-right pr-4 whitespace-nowrap">
                          <div className="flex items-center justify-end gap-1 opacity-80 group-hover:opacity-100 transition-opacity">
                            <button
                              type="button"
                              onClick={() => setActiveDoc(doc)}
                              className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
                              title="阅读解析全文与切片"
                            >
                              <ExternalLink className="h-3.5 w-3.5" />
                            </button>
                            <button
                              type="button"
                              onClick={() => handleReprocess(doc.id)}
                              className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
                              title="重新解析"
                            >
                              <RotateCw className="h-3.5 w-3.5" />
                            </button>
                            <button
                              type="button"
                              onClick={() => handleDelete(doc.id)}
                              className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-destructive"
                              title="删除文献"
                            >
                              <Trash2 className="h-3.5 w-3.5" />
                            </button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          {/* 分页契约：加载更多 */}
          {list.hasMore && (
            <div className="flex justify-center pt-4 pb-2">
              <Button
                variant="outline"
                size="sm"
                onClick={handleLoadMore}
                disabled={isLoadingMore}
                className="gap-2 text-xs"
              >
                {isLoadingMore ? (
                  <>
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    <span>正在加载更多文献...</span>
                  </>
                ) : (
                  <span>
                    加载更多文献 ({documents.length} / {list.total})
                  </span>
                )}
              </Button>
            </div>
          )}
        </FourStateView>

        <DocumentReaderModal
          document={activeDoc}
          isOpen={Boolean(activeDoc)}
          onClose={() => setActiveDoc(null)}
        />
        <DocumentUploadModal
          spaceId={spaceId}
          isOpen={isUploadOpen}
          onClose={() => setIsUploadOpen(false)}
          onUploaded={loadDocuments}
        />
      </div>
    </div>
  );
}
