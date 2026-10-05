import React, { useState, useEffect, useRef } from 'react';
import { formatShortcut } from '@/lib/platform';
import type { components } from '@/lib/api/types.gen';

type Conversation = components['schemas']['Conversation'];
import {
  usePaginatedSearch,
  type ListOptions,
} from '@/hooks/usePaginatedSearch';
import { chatService } from '@/lib/api/services/chat';
import { DEFAULT_CONVERSATION_TITLE } from '../lib/conversationTitle';
import { Button } from '@/components/ui/button';
import {
  Plus,
  Pin,
  MessageSquare,
  Search,
  Trash2,
  Loader2,
  Pencil,
  MoreHorizontal,
} from 'lucide-react';
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
} from '@/components/ui/dropdown-menu';
import { useAsyncTask } from '@/hooks/useAsyncTask';
import { cn } from '@/lib/utils';
import { toast } from 'sonner';
import { useConfirm } from '@/components/shared/ConfirmProvider';

interface ConversationListProps {
  spaceId: string | null;
  currentId: string | null;
  /** null 表示当前没有选中的会话（例如刚把选中的那条删掉了） */
  onSelect: (id: string | null) => void;
  onConversationsLoaded?: (convs: Conversation[]) => void;
  /** 用户手动改了名。ChatPage 据此更新本地副本，免得首问自动命名把新名字覆盖掉。 */
  onConversationRenamed?: (id: string, title: string) => void;
  /** 变一次就重拉一次列表。ChatPage 在首条提问把会话自动改名后用它刷新标题。 */
  refreshToken?: number;
}

const fetchConversationPage = (spaceId: string, options: ListOptions) =>
  chatService.getConversations(spaceId, options);

/**
 * memo 化：流式回答期间 ChatPage 每几十毫秒重渲染一次，会话列表不该跟着重算；
 * ChatPage 传下来的回调已保证身份稳定，props 不变时直接跳过渲染。
 */
export const ConversationList = React.memo(function ConversationList({
  spaceId,
  currentId,
  onSelect,
  onConversationsLoaded,
  onConversationRenamed,
  refreshToken = 0,
}: ConversationListProps) {
  const [filter, setFilter] = useState('');
  const [isCreating, setIsCreating] = useState(false);
  const { start: startCreate } = useAsyncTask(spaceId);
  useEffect(() => {
    setIsCreating(false);
  }, [spaceId]);
  const list = usePaginatedSearch({
    key: 'conversations',
    spaceId,
    search: filter,
    fetchPage: fetchConversationPage,
  });
  const conversations = list.items;
  const filtered = conversations;
  const loading = list.isLoading;
  const isLoadingMore = list.isLoadingMore;
  const loadFailed = Boolean(list.error);
  const refreshRef = useRef(refreshToken);
  // 行内重命名：正在改哪一条、输入框里的草稿
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState('');
  const renameInputRef = useRef<HTMLInputElement>(null);
  const requestConfirmation = useConfirm();
  // Enter 提交后输入框随即卸载，有的浏览器还会补发一次 blur——没有这道闸就会提交两遍
  const editSettledRef = useRef(false);

  // 回调与 currentId 走 ref，不进 loadConversations 的依赖。
  //
  // 它们原本是直接依赖的，而 ChatPage 传下来的 `onSelect` 是行内箭头函数、
  // `onConversationsLoaded` 也是每次渲染重建的——于是 ChatPage 每重渲染一次，
  // loadConversations 就换一个身份，useEffect 跟着重拉一次列表。实测单次进页面
  // 就发了 5 次请求；更糟的是流式回答时 ChatPage 每个 token 重渲染一次，
  // 一条长回答能打出成百上千次列表请求。
  const onSelectRef = useRef(onSelect);
  const onLoadedRef = useRef(onConversationsLoaded);
  const currentIdRef = useRef(currentId);
  useEffect(() => {
    onSelectRef.current = onSelect;
    onLoadedRef.current = onConversationsLoaded;
    currentIdRef.current = currentId;
  });

  useEffect(() => {
    if (list.query === '' && list.status === 'ready')
      onLoadedRef.current?.(conversations);
  }, [conversations, list.query, list.status]);

  useEffect(() => {
    if (refreshRef.current === refreshToken) return;
    refreshRef.current = refreshToken;
    void list.refresh();
  }, [refreshToken, list.refresh]);

  const handleLoadMore = async () => {
    try {
      await list.loadMore();
    } catch {
      toast.error('加载更多会话失败');
    }
  };

  const handleCreateNew = async () => {
    if (!spaceId) return;
    const task = startCreate();
    if (!task) return;
    setIsCreating(true);
    try {
      const newConv = await chatService.createConversation(spaceId, {
        title: DEFAULT_CONVERSATION_TITLE,
      });
      if (!task.current()) return;
      void list.refresh();
      onSelect(newConv.id);
      toast.success('已创建新对话');
    } catch {
      if (task.current()) toast.error('创建对话失败');
    } finally {
      if (task.finish()) setIsCreating(false);
    }
  };

  const handleTogglePin = async (e: React.MouseEvent, conv: Conversation) => {
    e.stopPropagation();
    try {
      const updated = await chatService.updateConversation(conv.id, {
        pinned: !conv.pinned,
      });
      list.updateItem(updated);
      void list.refresh();
    } catch {
      toast.error('更新置顶状态失败');
    }
  };

  const startRename = (e: React.MouseEvent, conv: Conversation) => {
    e.stopPropagation();
    editSettledRef.current = false;
    setDraftTitle(conv.title || DEFAULT_CONVERSATION_TITLE);
    setEditingId(conv.id);
  };

  const cancelRename = () => {
    editSettledRef.current = true;
    setEditingId(null);
  };

  /**
   * 保存行内编辑的标题。
   *
   * 标题原本只能由首条提问自动生成，起得不好（截在半句话上、几条会话撞名）也改不了。
   * 空标题不保存：侧栏会退回显示「新对话」，而首问自动命名又只认这个占位标题，
   * 清空等于把会话重新交给自动命名，和用户想「改个名」的意图正好相反。
   */
  const commitRename = async (conv: Conversation) => {
    if (editSettledRef.current) return;
    editSettledRef.current = true;
    setEditingId(null);
    const title = draftTitle.replace(/\s+/g, ' ').trim();
    if (!title) {
      toast.info('标题不能为空，已保留原名');
      return;
    }
    if (title === (conv.title || DEFAULT_CONVERSATION_TITLE)) return;
    try {
      const updated = await chatService.updateConversation(conv.id, { title });
      const saved = updated.title || title;
      list.updateItem(updated);
      void list.refresh();
      onConversationRenamed?.(conv.id, saved);
    } catch {
      toast.error('重命名失败，请稍后再试');
    }
  };

  const handleDelete = async (e: React.MouseEvent, conv: Conversation) => {
    e.stopPropagation();
    const convId = conv.id;
    // 文献删除一直有二次确认，会话却是点一下就没——而会话装着轨迹、引用与纠错反馈，
    // 正是进化闭环的原料。删除按钮又紧挨着置顶按钮（都只有 12px），误点代价太大。
    const accepted = await requestConfirmation({
      title: `删除对话「${conv.title || DEFAULT_CONVERSATION_TITLE}」？`,
      description: '该对话的消息、引用轨迹与反馈将一并移除，且无法恢复。',
      confirmText: '删除对话',
      destructive: true,
    });
    if (!accepted) {
      return;
    }
    try {
      await chatService.deleteConversation(convId);
      const remaining = conversations.filter((c) => c.id !== convId);
      await list.removeItems([convId]);
      // 删的是当前会话：有别的就切过去，没有就明确清空选中——留着已删的 id，
      // 之后每次提问都会发往一个不存在的会话（404）
      if (currentId === convId) {
        onSelect(remaining.length > 0 ? remaining[0].id : null);
      }
      toast.success('已删除对话');
    } catch {
      toast.error('删除对话失败');
    }
  };

  return (
    <div className="flex h-full min-h-0 w-full shrink-0 flex-col border-r border-border bg-card/40 select-none min-[1100px]:w-[244px]">
      {/* 头部操作与搜索 */}
      <div className="p-4 space-y-3 border-b border-border/50">
        <Button
          onClick={handleCreateNew}
          className="w-full justify-start gap-2 bg-primary/10 text-primary hover:bg-primary/20 hover:text-primary"
          variant="outline"
          size="sm"
          disabled={!spaceId || loading || isCreating}
        >
          {isCreating ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <Plus className="h-4 w-4" />
          )}
          <span>{isCreating ? '正在创建…' : '新建对话'}</span>
          <span className="ml-auto text-[10px] opacity-70">{formatShortcut('⌘N')}</span>
        </Button>

        <div className="relative">
          <Search className="absolute left-2.5 top-2.5 h-3.5 w-3.5 text-muted-foreground" />
          <input
            type="text"
            aria-label="搜索历史对话"
            maxLength={200}
            placeholder="搜索全部历史对话..."
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            className="w-full rounded-md border border-border bg-background/80 py-1.5 pl-8 pr-2.5 text-xs text-foreground placeholder:text-muted-foreground outline-none focus:border-primary/50"
          />
        </div>
      </div>

      <div className="flex items-center justify-between px-4 pt-4 pb-2 text-[10px] font-medium text-muted-foreground">
        <span>{filter.trim() ? '搜索结果' : '最近的讨论'}</span>
        <span>{filtered.length}</span>
      </div>
      {/* 对话列表 */}
      <div className="flex-1 overflow-y-auto p-2 space-y-1">
        {loadFailed && conversations.length === 0 && (
          <div className="px-2.5 py-3 text-[11px] leading-relaxed text-destructive">
            会话列表加载失败，请检查后端是否在运行。这里的空白不代表历史对话已丢失。
          </div>
        )}
        {loading && (
          <p role="status" className="px-3 py-2 text-xs text-muted-foreground">
            {filter.trim() ? '正在搜索对话…' : '正在加载对话…'}
          </p>
        )}
        {!loading && !loadFailed && filtered.length === 0 && (
          <p role="status" className="px-3 py-3 text-xs text-muted-foreground">
            {filter.trim()
              ? '没有匹配的历史对话'
              : '还没有对话，开始第一个问题吧。'}
          </p>
        )}
        {loadFailed && (
          <button
            type="button"
            onClick={() => void list.refresh()}
            className="px-3 py-2 text-xs text-primary underline"
          >
            重试加载
          </button>
        )}
        {filtered.map((conv) => {
          const isActive = conv.id === currentId;
          const isEditing = conv.id === editingId;
          return (
            <div
              key={conv.id}
              onClick={() => onSelect(conv.id)}
              className={cn(
                'group flex cursor-pointer items-center justify-between gap-1 rounded-lg px-2.5 py-2 text-xs transition-colors',
                isActive
                  ? 'bg-primary/10 text-primary font-medium shadow-2xs'
                  : 'text-muted-foreground hover:bg-muted/60 hover:text-foreground',
              )}
            >
              <div className="flex min-w-0 flex-1 items-center gap-2">
                <MessageSquare className="h-3.5 w-3.5 shrink-0" />
                {conv.pinned && (
                  <Pin
                    className="h-3 w-3 shrink-0 text-primary"
                    aria-label="已置顶"
                  />
                )}
                {isEditing ? (
                  <input
                    type="text"
                    autoFocus
                    ref={renameInputRef}
                    value={draftTitle}
                    maxLength={100}
                    aria-label="会话标题"
                    onChange={(e) => setDraftTitle(e.target.value)}
                    onFocus={(e) => e.target.select()}
                    onClick={(e) => e.stopPropagation()}
                    onDoubleClick={(e) => e.stopPropagation()}
                    onKeyDown={(e) => {
                      // 输入法组字时的 Enter 是选词，不是提交
                      if (e.nativeEvent.isComposing) return;
                      if (e.key === 'Enter') {
                        e.preventDefault();
                        e.stopPropagation();
                        commitRename(conv);
                      } else if (e.key === 'Escape') {
                        e.preventDefault();
                        e.stopPropagation();
                        cancelRename();
                      }
                    }}
                    onBlur={() => commitRename(conv)}
                    className="min-w-0 flex-1 rounded border border-primary/50 bg-background px-1.5 py-0.5 text-xs text-foreground outline-none select-text"
                  />
                ) : (
                  <button
                    type="button"
                    aria-label={`打开对话：${conv.title || DEFAULT_CONVERSATION_TITLE}`}
                    aria-current={isActive ? 'true' : undefined}
                    className="min-h-9 min-w-0 flex-1 truncate rounded text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
                    onClick={(e) => {
                      e.stopPropagation();
                      onSelect(conv.id);
                    }}
                    title="双击重命名"
                    onDoubleClick={(e) => startRename(e, conv)}
                  >
                    {conv.title || DEFAULT_CONVERSATION_TITLE}
                  </button>
                )}
              </div>
              <DropdownMenu>
                <DropdownMenuTrigger
                  aria-label={`对话操作：${conv.title || DEFAULT_CONVERSATION_TITLE}`}
                  title="对话操作"
                  onClick={(event) => event.stopPropagation()}
                  className={cn(
                    'flex h-9 w-9 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary',
                    isEditing && 'hidden',
                  )}
                >
                  <MoreHorizontal className="h-4 w-4" />
                </DropdownMenuTrigger>
                <DropdownMenuContent
                  align="end"
                  finalFocus={() =>
                    editingId === conv.id ? renameInputRef.current : true
                  }
                  onClick={(event) => event.stopPropagation()}
                  className="min-w-40"
                >
                  <DropdownMenuItem
                    onClick={(event) => startRename(event, conv)}
                    className="min-h-9 gap-2"
                  >
                    <Pencil className="h-4 w-4" />
                    重命名
                  </DropdownMenuItem>
                  <DropdownMenuItem
                    onClick={(event) => handleTogglePin(event, conv)}
                    className="min-h-9 gap-2"
                  >
                    <Pin className="h-4 w-4" />
                    {conv.pinned ? '取消置顶' : '置顶对话'}
                  </DropdownMenuItem>
                  <DropdownMenuSeparator />
                  <DropdownMenuItem
                    variant="destructive"
                    onClick={(event) => handleDelete(event, conv)}
                    className="min-h-9 gap-2"
                  >
                    <Trash2 className="h-4 w-4" />
                    删除对话
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            </div>
          );
        })}

        {/* 分页契约：加载更多 */}
        {list.hasMore && (
          <div className="pt-2 pb-1 px-1">
            <Button
              variant="ghost"
              size="sm"
              onClick={handleLoadMore}
              disabled={isLoadingMore}
              className="w-full text-xs text-muted-foreground hover:text-foreground h-7 gap-1.5"
            >
              {isLoadingMore ? (
                <>
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  <span>正在加载...</span>
                </>
              ) : (
                <span>加载更多会话</span>
              )}
            </Button>
          </div>
        )}
      </div>
    </div>
  );
});
