import React, { useState, useEffect, useRef, useMemo, useCallback } from 'react';
import { useParams, useNavigate, useSearchParams } from 'react-router';
import { ConversationList } from './components/ConversationList';
import { ContextUsageSummary } from './components/ContextUsageSummary';
import { MessageList } from './components/MessageList';
import { ChatInputArea } from './components/ChatInputArea';
import { EvidenceSidebar } from './components/EvidenceSidebar';
import { FourStateView } from '@/components/shared/FourStateView';
import { useChatStream } from './hooks/useChatStream';
import {
  DEFAULT_CONVERSATION_TITLE,
  deriveConversationTitle,
} from './lib/conversationTitle';
import { chatService } from '@/lib/api/services/chat';
import { documentService } from '@/lib/api';
import { buildStarterPrompts, type StarterPrompt } from './lib/starterPrompts';
import { numberCitations } from './lib/citations';
import {
  buildConversationMarkdown,
  conversationExportFileName,
} from './lib/exportMarkdown';
import { useUiStore } from '@/stores/useUiStore';
import { useMediaQuery } from '@/hooks/useMediaQuery';
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from '@/components/ui/sheet';
import { useSpaceStore } from '@/stores/useSpaceStore';
import { toast } from 'sonner';
import {
  MessageSquarePlus,
  ArrowRight,
  BookOpen,
  FileText,
  Download,
  History,
  PanelRight,
  Plus,
  Sparkles,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import type { components } from '@/lib/api/types.gen';

type Conversation = components['schemas']['Conversation'];

const INITIAL_VISIBLE_COUNT = 60;
const BATCH_SIZE = 60;

export function ChatPage() {
  const navigate = useNavigate();
  const { spaceId: paramSpaceId } = useParams();
  const { currentSpaceId, getCurrentSpace } = useSpaceStore();
  const spaceId = paramSpaceId || currentSpaceId;
  const currentSpace = getCurrentSpace();
  const [searchParams, setSearchParams] = useSearchParams();
  const conversationInitialized = useRef(searchParams.get('new') === '1');
  const hasEvidencePanel = useMediaQuery('(min-width: 1440px)');
  const [historyOpen, setHistoryOpen] = useState(false);
  const {
    isEvidenceOpen,
    toggleEvidence,
    setEvidenceOpen,
    setActiveConversationId,
    bumpConversations,
  } = useUiStore();

  const startingSendRef = useRef(false);
  const [isStartingSend, setIsStartingSend] = useState(false);
  const [contextMode, setContextMode] = useState<'standard' | 'economy'>(
    'standard',
  );
  const [selectedConvId, setSelectedConvId] = useState<string | null>(null);
  const [visibleCount, setVisibleCount] = useState<number>(
    INITIAL_VISIBLE_COUNT,
  );
  // 变一次，侧栏重拉一次列表（自动命名、新建会话后用）
  const [convRefreshToken, setConvRefreshToken] = useState(0);
  const conversationsRef = useRef<Conversation[]>([]);
  // 记下「这个会话刚在本地建出来，别去拉它的历史」
  const skipHistoryForRef = useRef<string | null>(null);

  // 滚动（到底、载入更早时保持位置）全由 MessageList 里的 Virtuoso 负责
  const prevMessagesLenRef = useRef<number>(0);
  const lastConvIdRef = useRef<string | null>(null);

  const {
    messages,
    isStreaming,
    isHistoryLoading,
    processStages,
    activeTrace,
    contextUsage,
    focusTrace,
    sendMessage,
    abortStream,
    loadConversationHistory,
    resetConversation,
  } = useChatStream({ spaceId, conversationId: selectedConvId });

  useEffect(() => {
    if (!hasEvidencePanel) setEvidenceOpen(false);
  }, [hasEvidencePanel, setEvidenceOpen]);

  useEffect(() => {
    if (searchParams.get('new') !== '1') return;
    conversationInitialized.current = true;
    setSelectedConvId(null);
    resetConversation();
    const next = new URLSearchParams(searchParams);
    next.delete('new');
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams, resetConversation]);

  // 侧栏的最近对话卡片带着 ?c=<id> 过来：打开那段对话；?history=1 打开全部对话
  useEffect(() => {
    const target = searchParams.get('c');
    const wantsHistory = searchParams.get('history') === '1';
    if (!target && !wantsHistory) return;
    if (target) {
      conversationInitialized.current = true;
      setSelectedConvId(target);
    }
    if (wantsHistory) setHistoryOpen(true);
    const next = new URLSearchParams(searchParams);
    next.delete('c');
    next.delete('history');
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams]);

  // 侧栏据此高亮当前对话；列表有变化时通知它重拉
  useEffect(() => {
    setActiveConversationId(selectedConvId);
  }, [selectedConvId, setActiveConversationId]);
  useEffect(() => () => setActiveConversationId(null), [setActiveConversationId]);
  useEffect(() => {
    if (convRefreshToken > 0) bumpConversations();
  }, [convRefreshToken, bumpConversations]);

  // 起手问题取自这个空间已就绪的资料；资料数变了（刚导入完）就重新取
  const [starterPrompts, setStarterPrompts] = useState<StarterPrompt[]>([]);
  const docCount = currentSpace?.doc_count ?? 0;
  useEffect(() => {
    if (!spaceId) return;
    let cancelled = false;
    setStarterPrompts([]);
    documentService
      .getDocuments(spaceId, { limit: 10, status: 'ready' })
      .then((res) => {
        if (!cancelled) setStarterPrompts(buildStarterPrompts(res.items || []));
      })
      .catch(() => {
        // 取不到就不推荐，不拿写死的示例问题顶上
        if (!cancelled) setStarterPrompts([]);
      });
    return () => {
      cancelled = true;
    };
  }, [spaceId, docCount]);

  // 证据栏只列这次回答真正引用了的资料。轨迹对应的回答：流式中按 message_id 找
  // （trace_id 要到 done 才写回消息），从历史加载的按 trace_id 找。
  // 还在生成、模型没落引用之前返回 null——此时说不清哪条会被用到，先全列出来。
  const citedChunkIds = useMemo(() => {
    if (!activeTrace) return null;
    const answer = messages.find(
      (msg) =>
        msg.role === 'assistant' &&
        ((activeTrace.id && msg.trace_id === activeTrace.id) ||
          msg.id === activeTrace.message_id),
    );
    if (!answer) return null;
    const cited = (answer.citations || [])
      .map((c) => c.chunk_id)
      .filter(Boolean);
    if (isStreaming && cited.length === 0) return null;
    return cited;
  }, [activeTrace, messages, isStreaming]);

  // 证据栏卡片上的「引用 N」要与回答正文里的角标同号
  const citationNumbers = useMemo(() => {
    if (!activeTrace) return null;
    const answer = messages.find(
      (msg) =>
        msg.role === 'assistant' &&
        ((activeTrace.id && msg.trace_id === activeTrace.id) ||
          msg.id === activeTrace.message_id),
    );
    return answer ? numberCitations(answer.citations).byChunk : null;
  }, [activeTrace, messages]);

  // 监听选中对话变化，拉取历史消息
  useEffect(() => {
    if (!selectedConvId) {
      resetConversation();
      return;
    }
    // 刚在本地新建并已经乐观插入了消息的会话，服务端还没有历史可拉；
    // 这时候去拉会用一个空列表把正在流式生成的消息冲掉
    if (skipHistoryForRef.current === selectedConvId) {
      skipHistoryForRef.current = null;
      return;
    }
    loadConversationHistory(selectedConvId);
  }, [selectedConvId, loadConversationHistory, resetConversation]);

  // 切换会话时重置为最近 60 条
  useEffect(() => {
    if (selectedConvId !== lastConvIdRef.current) {
      lastConvIdRef.current = selectedConvId;
      setVisibleCount(INITIAL_VISIBLE_COUNT);
      prevMessagesLenRef.current = 0;
    }
  }, [selectedConvId]);

  // 在同一会话中新增消息（提问或回复流）时，扩充可见数避免顶部渲染项被移出 DOM
  useEffect(() => {
    if (
      prevMessagesLenRef.current > 0 &&
      messages.length > prevMessagesLenRef.current
    ) {
      const added = messages.length - prevMessagesLenRef.current;
      setVisibleCount((prev) => prev + added);
    }
    prevMessagesLenRef.current = messages.length;
  }, [messages.length]);

  // 点「载入更早」：多渲染一批。视口停留由 Virtuoso 的 firstItemIndex 负责
  const handleLoadEarlier = () => {
    setVisibleCount((prev) => prev + BATCH_SIZE);
  };

  /** 侧栏里改了名：同步到本地副本，否则首问自动命名会把用户刚起的名字当成占位标题覆盖掉。 */
  const handleConversationRenamed = useCallback((id: string, title: string) => {
    conversationsRef.current = conversationsRef.current.map((c) =>
      c.id === id ? { ...c, title } : c,
    );
    setConversationList(conversationsRef.current);
    bumpConversations();
  }, [bumpConversations]);

  /**
   * 把当前会话导出成 Markdown 下载。
   *
   * 纯前端生成：消息与引用都已经在页面上了，不必为此再开一个接口。
   * 流式生成中不让导，半截回答导出去会被当成完整结论。
   */
  const handleExport = () => {
    if (!selectedConvId || messages.length === 0 || isStreaming) return;
    const title = conversationsRef.current.find(
      (c) => c.id === selectedConvId,
    )?.title;
    const now = new Date();
    const markdown = buildConversationMarkdown({
      title,
      messages,
      spaceName: currentSpace?.name,
      exportedAt: now,
    });
    const url = URL.createObjectURL(
      new Blob([markdown], { type: 'text/markdown;charset=utf-8' }),
    );
    const link = document.createElement('a');
    link.href = url;
    link.download = conversationExportFileName(title, now);
    document.body.appendChild(link);
    link.click();
    link.remove();
    // 立刻 revoke 在部分浏览器里会让下载拿到空文件，留一拍再回收
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast.success('已导出为 Markdown');
  };

  const [conversationList, setConversationList] = useState<Conversation[]>([]);
  const handleConversationsLoaded = useCallback(
    (convs: Conversation[]) => {
      conversationsRef.current = convs;
      setConversationList(convs);
      if (
        convs.length > 0 &&
        !selectedConvId &&
        !conversationInitialized.current
      ) {
        conversationInitialized.current = true;
        setSelectedConvId(convs[0].id);
      }
    },
    [selectedConvId],
  );

  // 会话列表不再常驻页面左栏（侧栏已列出最近对话，全部对话收进抽屉），
  // 默认打开最近一段对话、导出取标题都靠这份列表，所以页面自己拉一次
  useEffect(() => {
    if (!spaceId) return;
    const controller = new AbortController();
    chatService
      .getConversations(spaceId, { limit: 50, signal: controller.signal })
      .then((page) => handleConversationsLoaded(page.items))
      .catch(() => {
        // 拉不到就停在新对话：输入框照常可用，发问时会新建会话
      });
    return () => controller.abort();
  }, [spaceId, convRefreshToken, handleConversationsLoaded]);

  const currentTitle =
    conversationList.find((c) => c.id === selectedConvId)?.title ??
    (selectedConvId ? '' : '新对话');

  /**
   * 提问的统一入口，补上两件原本缺的事：
   *
   * 1. **没有会话时先建一个。** `sendMessage` 遇到空 conversationId 直接 return，
   *    而全新 Space 本来就一条会话都没有——首次启动点推荐问题卡、或在输入框里敲回车，
   *    都是毫无反应的死路（没有消息、没有报错、没有提示）。
   * 2. **首条提问后按内容命名会话。** 创建路径把标题写死成「新对话」，前后端都不会
   *    依据内容改名，也没有重命名入口，侧栏于是变成一排无法分辨的同名项。
   */
  const handleSend = async (
    content: string,
    options?: {
      useRetrieval?: boolean;
      useInsights?: boolean;
      searchMode?: 'hybrid' | 'vector';
      contextMode?: 'standard' | 'economy';
    },
  ) => {
    if (
      !content.trim() ||
      isStreaming ||
      isHistoryLoading ||
      startingSendRef.current
    )
      return;
    startingSendRef.current = true;
    conversationInitialized.current = true;
    setIsStartingSend(true);
    try {
      let convId = selectedConvId;
      let isFirstMessage = messages.length === 0;

      if (!convId) {
        if (!spaceId) return;
        try {
          const created = await chatService.createConversation(spaceId, {
            title: DEFAULT_CONVERSATION_TITLE,
          });
          convId = created.id;
          isFirstMessage = true;
          // 刚建出来的会话服务端还没有历史，去拉一次只会把乐观插入的消息冲掉
          skipHistoryForRef.current = created.id;
          setSelectedConvId(created.id);
          setConvRefreshToken((n) => n + 1);
        } catch {
          toast.error('创建对话失败，无法发送提问');
          return;
        }
      }

      sendMessage(content, { contextMode, ...options, conversationId: convId });

      if (!isFirstMessage) return;
      // 只改还挂着占位标题的会话，不覆盖已经有名字的
      const existing = conversationsRef.current.find((c) => c.id === convId);
      if (
        existing &&
        existing.title &&
        existing.title !== DEFAULT_CONVERSATION_TITLE
      )
        return;
      const title = deriveConversationTitle(content);
      if (!title) return;
      try {
        await chatService.updateConversation(convId, { title });
        setConvRefreshToken((n) => n + 1);
      } catch {
        // 改名失败不该影响提问本身，标题留着占位即可
      }
    } finally {
      startingSendRef.current = false;
      setIsStartingSend(false);
    }
  };

  /**
   * 用同一个问题再问一次。
   *
   * 「重新生成」按钮此前只弹一句 `toast.info('触发重新生成')`，什么都不做。
   * 这里往前找到这条回答对应的那次提问，原样重发——历史不改写，
   * 新答案追加在末尾，方便和旧答案对照（换了经验或模型之后尤其有用）。
   */
  const handleRegenerate = (assistantMessageId: string) => {
    if (isStreaming) return;
    const index = messages.findIndex((m) => m.id === assistantMessageId);
    if (index <= 0) return;
    for (let i = index - 1; i >= 0; i -= 1) {
      if (messages[i].role === 'user') {
        handleSend(messages[i].content);
        return;
      }
    }
    toast.error('找不到这条回答对应的提问，无法重新生成');
  };
  // memo 化的消息气泡要求回调身份稳定。handleRegenerate 依赖 messages/isStreaming，
  // 直接 useCallback 会在流式期间每帧换新引用、memo 随之失效，所以经 ref 转发
  const regenerateRef = useRef(handleRegenerate);
  regenerateRef.current = handleRegenerate;
  const stableRegenerate = useCallback(
    (assistantMessageId: string) => regenerateRef.current(assistantMessageId),
    [],
  );

  const handleSelectFromSheet = useCallback((id: string | null) => {
    setSelectedConvId(id);
    setHistoryOpen(false);
  }, []);

  const pageStatus = !spaceId ? 'empty' : 'ready';

  const startIndex = Math.max(0, messages.length - visibleCount);
  const remainingCount = startIndex;

  return (
    <div className="flex h-full w-full overflow-hidden">
      <Sheet open={historyOpen} onOpenChange={setHistoryOpen}>
        <SheetContent
          side="left"
          className="gap-0 p-0 data-[side=left]:w-[min(340px,92vw)]"
        >
          <SheetHeader>
            <SheetTitle>全部对话</SheetTitle>
            <SheetDescription>
              搜索、置顶、重命名，或继续之前的讨论。
            </SheetDescription>
          </SheetHeader>
          <ConversationList
            spaceId={spaceId}
            currentId={selectedConvId}
            onSelect={handleSelectFromSheet}
            onConversationsLoaded={handleConversationsLoaded}
            onConversationRenamed={handleConversationRenamed}
            refreshToken={convRefreshToken}
          />
        </SheetContent>
      </Sheet>

      {/* 中间主聊天工作区 */}
      <div className="flex flex-1 flex-col overflow-hidden min-w-0 bg-background/50">
        <FourStateView
          status={pageStatus}
          emptyTitle="开启一场专家级对话"
          emptyDescription="先选择或新建一个知识空间，AgentMem 会检索空间里的资料并结合沉淀的经验作答。"
          emptyActionLabel="选择知识空间"
          emptyIcon={<MessageSquarePlus className="h-8 w-8 text-primary" />}
          onEmptyAction={() => navigate('/')}
        >
          <div className="flex h-14 shrink-0 items-center justify-between gap-3 px-3 sm:px-5">
            <div className="flex min-w-0 items-center gap-1.5">
              <Button
                variant="ghost"
                size="icon"
                aria-label="打开全部对话"
                title="全部对话"
                className="h-8 w-8 text-muted-foreground"
                onClick={() => setHistoryOpen(true)}
              >
                <History className="h-4 w-4" />
              </Button>
              <h1 className="min-w-0 truncate text-[14px] font-medium text-foreground">
                {currentTitle || '知识对话'}
              </h1>
            </div>
            <div className="flex shrink-0 items-center gap-1.5">
              <Button
                variant="ghost"
                size="sm"
                aria-label="开始新对话"
                onClick={() => {
                  conversationInitialized.current = true;
                  setSelectedConvId(null);
                  resetConversation();
                }}
                disabled={isStreaming}
                className="gap-1.5 text-xs"
              >
                <Plus className="h-3.5 w-3.5" />
                <span className="hidden sm:inline">新对话</span>
              </Button>
              <Button
                variant="ghost"
                size="sm"
                className="gap-1.5 text-xs"
                onClick={handleExport}
                disabled={!selectedConvId || messages.length === 0 || isStreaming}
                aria-label="导出对话"
                title={
                  isStreaming
                    ? '回答生成完再导出'
                    : '把当前对话（含引用来源）导出为 Markdown 文件'
                }
              >
                <Download className="h-3.5 w-3.5" />
                <span className="hidden sm:inline">导出</span>
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={toggleEvidence}
                aria-label={isEvidenceOpen ? '收起引用来源' : '打开引用来源'}
                aria-expanded={isEvidenceOpen}
                className="gap-1.5 text-xs"
              >
                <PanelRight className="h-3.5 w-3.5" />
                <span>来源</span>
              </Button>
            </div>
          </div>
          {/* 消息滚动区。有消息时走虚拟列表：一条回答就是一整块 Markdown，
              长会话全挂在 DOM 上会滚得发粘 */}
          <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
            {messages.length === 0 ? (
              <div className="flex-1 overflow-y-auto px-4 py-2 md:px-8">
                <div className="mx-auto max-w-4xl space-y-4">
                  <div className="flex flex-col items-center justify-center px-2 py-10 sm:py-16 select-none">
                    <div className="mb-6 flex h-16 w-16 items-center justify-center rounded-2xl border border-primary/15 bg-primary/8 text-primary shadow-sm">
                      <Sparkles className="h-7 w-7" />
                    </div>
                    <span className="mb-3 text-xs font-medium tracking-wide text-primary">
                      {currentSpace?.name || '知识空间'}
                    </span>
                    <h2 className="text-center text-2xl font-semibold tracking-tight text-foreground sm:text-3xl">
                      从一个好问题，开始新的发现
                    </h2>
                    <p className="mt-3 max-w-md text-center text-sm leading-relaxed text-muted-foreground">
                      输入问题，系统会检索这个空间的资料与知识卡片，并结合沉淀下来的经验作答。
                    </p>

                    {/* 新空间的第一步：引导导入资料（不是警告——空空间是正常的起点） */}
                    {currentSpace && (currentSpace.doc_count ?? 0) === 0 && (
                      <div className="mt-5 w-full max-w-md rounded-xl border border-primary/25 bg-primary/5 p-4 text-left">
                        <div className="flex items-center gap-2 text-sm font-semibold text-foreground">
                          <BookOpen className="h-4 w-4 text-primary shrink-0" />
                          第一步：导入这个领域的资料
                        </div>
                        <p className="mt-1.5 text-xs leading-relaxed text-muted-foreground">
                          文档、笔记、网页都可以。导入后回答会引用原文并标出处；现在直接提问也行，只是只能依赖模型自己的知识。
                        </p>
                        <Button
                          size="sm"
                          className="mt-3 h-8 gap-1.5 text-xs"
                          onClick={() =>
                            navigate(`/s/${spaceId}/library?import=1`)
                          }
                        >
                          <BookOpen className="h-3.5 w-3.5" />
                          导入资料
                        </Button>
                      </div>
                    )}

                    {/* 起手问题：取自本空间的资料，没有资料就不显示 */}
                    {starterPrompts.length > 0 && (
                      <div className="mt-8 grid w-full max-w-2xl grid-cols-1 gap-3 md:grid-cols-3">
                        {starterPrompts.map((item) => {
                          return (
                            <button
                              key={item.title}
                              type="button"
                              onClick={() => handleSend(item.query)}
                              className="group relative flex flex-col justify-between rounded-xl border border-border/80 bg-card p-4 text-left transition-all hover:border-primary/50 hover:bg-card hover:shadow-xs cursor-pointer"
                            >
                              <div>
                                <div className="flex items-center gap-2 text-primary">
                                  <FileText className="h-4 w-4 shrink-0" />
                                  <span className="font-semibold text-xs text-foreground line-clamp-1">
                                    {item.title}
                                  </span>
                                </div>
                                <p className="mt-2 text-[11px] text-muted-foreground leading-relaxed line-clamp-2">
                                  {item.desc}
                                </p>
                              </div>
                              <div className="mt-3 flex items-center gap-1 text-[10px] font-medium text-primary opacity-70 group-hover:opacity-100 transition-opacity">
                                <span>一键提问</span>
                                <ArrowRight className="h-3 w-3 transition-transform group-hover:translate-x-0.5" />
                              </div>
                            </button>
                          );
                        })}
                      </div>
                    )}
                  </div>
                </div>
              </div>
            ) : (
              <MessageList
                messages={messages}
                startIndex={startIndex}
                remainingCount={remainingCount}
                isStreaming={isStreaming}
                conversationId={selectedConvId}
                onLoadEarlier={handleLoadEarlier}
                onFocusTrace={focusTrace}
                onRegenerate={stableRegenerate}
                liveStages={processStages}
                activeTrace={activeTrace}
              />
            )}
          </div>

          {/* 底部输入框 */}
          <ContextUsageSummary usage={contextUsage} />
          <ChatInputArea
            onSend={handleSend}
            onAbort={abortStream}
            contextMode={contextMode}
            onContextModeChange={setContextMode}
            isStreaming={isStreaming}
            isBusy={isHistoryLoading || isStartingSend}
          />
        </FourStateView>
      </div>

      {hasEvidencePanel ? (
        <EvidenceSidebar
          retrievedChunks={activeTrace?.retrieved}
          citedChunkIds={citedChunkIds}
          citationNumbers={citationNumbers}
          usedInsights={activeTrace?.used_insights}
          trace={activeTrace}
        />
      ) : (
        <Sheet open={isEvidenceOpen} onOpenChange={setEvidenceOpen}>
          <SheetContent
            side="right"
            className="gap-0 p-0 data-[side=right]:w-[min(400px,95vw)]"
            showCloseButton={false}
          >
            <SheetHeader className="sr-only">
              <SheetTitle>引用来源与执行轨迹</SheetTitle>
              <SheetDescription>
                查看回答所依据的资料、经验与检索过程。
              </SheetDescription>
            </SheetHeader>
            <EvidenceSidebar
              compact
              retrievedChunks={activeTrace?.retrieved}
              citedChunkIds={citedChunkIds}
              citationNumbers={citationNumbers}
              usedInsights={activeTrace?.used_insights}
              trace={activeTrace}
            />
          </SheetContent>
        </Sheet>
      )}
    </div>
  );
}
