import { useState, useCallback, useRef, useEffect } from 'react';
import type {
  Message,
  ProcessStage,
  TraceDetail,
  CitationMarker,
  ContextUsage,
} from '@/lib/api/types.temp';
import { mockTraceDetail } from '@/lib/api/mock/data';
import { chatService } from '@/lib/api/services/chat';
import { isMockMode } from '@/lib/api/client';
import { toast } from 'sonner';

const initialStages = (): ProcessStage[] => [
  { id: 'rewrite', label: '查询改写', status: 'pending' },
  { id: 'retrieval', label: '混合检索', status: 'pending' },
  { id: 'insights', label: '经验注入', status: 'pending' },
  { id: 'generating', label: '模型生成', status: 'pending' },
];

function pause(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) return reject(signal.reason);
    const abort = () => {
      clearTimeout(timer);
      signal.removeEventListener('abort', abort);
      reject(signal.reason);
    };
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', abort);
      resolve();
    }, ms);
    signal.addEventListener('abort', abort, { once: true });
  });
}

/**
 * 流式文本的合并写入器。
 *
 * token 的到达频率远高于屏幕刷新率（mock 逐字 18ms，真实后端按网络分片突发），
 * 逐 token setState 会让整棵聊天树跟着每个 token 重渲染。这里把同一窗口里的增量
 * 合并，最多每 50ms 落一次 state：视觉上看不出差别，渲染次数却从「每 token 一次」
 * 封顶到「每秒约 20 次」。终止、报错、手动停止时用 flushNow 把残余先落地，
 * 避免最后一段回答丢失。
 */
const STREAM_FLUSH_MS = 50;

interface StreamWriter {
  write: (text: string) => void;
  flushNow: () => void;
  cancel: () => void;
}

function createStreamWriter(flush: (text: string) => void): StreamWriter {
  let pending = '';
  let timer: ReturnType<typeof setTimeout> | null = null;
  const doFlush = () => {
    timer = null;
    flush(pending);
  };
  return {
    write(text) {
      pending = text;
      if (timer === null) timer = setTimeout(doFlush, STREAM_FLUSH_MS);
    },
    flushNow() {
      if (timer !== null) {
        clearTimeout(timer);
        timer = null;
      }
      doFlush();
    },
    cancel() {
      if (timer !== null) {
        clearTimeout(timer);
        timer = null;
      }
      pending = '';
    },
  };
}

interface UseChatStreamOptions {
  spaceId: string | null;
  conversationId: string | null;
}

export function useChatStream({
  spaceId,
  conversationId,
}: UseChatStreamOptions) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [processStages, setProcessStages] =
    useState<ProcessStage[]>(initialStages);
  const [isHistoryLoading, setIsHistoryLoading] = useState(false);
  const historyLoadingRef = useRef(false);
  const mountedRef = useRef(true);
  const streamingRef = useRef(false);
  const streamSeqRef = useRef(0);
  const traceSeqRef = useRef(0);
  const streamConversationRef = useRef<string | null>(null);
  const [activeTrace, setActiveTrace] = useState<TraceDetail | null>(null);
  const [contextUsage, setContextUsage] = useState<ContextUsage | null>(null);
  const abortControllerRef = useRef<AbortController | null>(null);
  /** 当前流的合并写入器：手动停止时先把残余文本落地，再作废弃流。 */
  const streamWriterRef = useRef<StreamWriter | null>(null);
  /** 第几次拉历史：只认最后一次，先点 A 再点 B 时 A 的慢响应不能覆盖 B。 */
  const historySeqRef = useRef(0);

  const invalidateStream = useCallback(() => {
    streamSeqRef.current += 1;
    abortControllerRef.current?.abort();
    abortControllerRef.current = null;
    streamConversationRef.current = null;
    streamingRef.current = false;
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      historySeqRef.current += 1;
      traceSeqRef.current += 1;
      invalidateStream();
    };
  }, [invalidateStream]);

  const loadConversationHistory = useCallback(
    async (convId: string) => {
      invalidateStream();
      setIsStreaming(false);
      setMessages([]);
      setActiveTrace(null);
      setContextUsage(null);
      setProcessStages(initialStages());
      historyLoadingRef.current = true;
      setIsHistoryLoading(true);
      const seq = ++historySeqRef.current;
      const isLatest = () =>
        mountedRef.current && seq === historySeqRef.current;
      try {
        const res = await chatService.getConversation(convId);
        if (!isLatest()) return;
        const loaded = res.messages || [];
        setMessages(loaded);
        historyLoadingRef.current = false;
        setIsHistoryLoading(false);
        // 历史消息带着 trace_id：把最后一条回答的轨迹拉回来，否则「在右侧证据栏定位」
        // 点开是空的——资料与经验都只存在于这次会话的实时流里
        const lastTraceId = [...loaded]
          .reverse()
          .find((msg) => msg.trace_id)?.trace_id;
        if (lastTraceId) {
          const traceSeq = ++traceSeqRef.current;
          try {
            const trace = await chatService.getTrace(lastTraceId);
            if (isLatest() && traceSeq === traceSeqRef.current)
              setActiveTrace(trace);
          } catch (err: unknown) {
            console.error('Failed to load trace:', err);
            if (isLatest() && traceSeq === traceSeqRef.current)
              setActiveTrace(null);
          }
        } else {
          setActiveTrace(null);
        }
      } catch (err: unknown) {
        console.error('Failed to load conversation history:', err);
        if (!isLatest()) return;
        // 加载失败不能留着上一个会话的消息：看起来像「加载成功但内容不对」，
        // 在这上面接着提问还会把问题写进当前选中的会话
        setMessages([]);
        setActiveTrace(null);
        toast.error('这个对话的历史加载失败，请检查后端是否在运行后重新点开');
      } finally {
        if (isLatest()) {
          historyLoadingRef.current = false;
          setIsHistoryLoading(false);
        }
      }
    },
    [invalidateStream],
  );

  /** 当前会话没了（被删、或切走）：清空消息与证据栏，作废还在路上的历史请求。 */
  const resetConversation = useCallback(() => {
    historySeqRef.current += 1;
    traceSeqRef.current += 1;
    invalidateStream();
    historyLoadingRef.current = false;
    setIsHistoryLoading(false);
    setProcessStages(initialStages());
    setMessages([]);
    setActiveTrace(null);
    setContextUsage(null);
    setIsStreaming(false);
  }, [invalidateStream]);

  /**
   * 把证据栏切到某条回答的轨迹上。
   *
   * 证据栏只放得下一份轨迹，此前固定是**最后一次**回答的那份：点更早那条回答里的
   * 引用，右侧列表里根本没有那个切片，于是「定位」点下去什么都不会发生。点哪条就
   * 把哪条的轨迹拉回来，定位才有落点。
   */
  const focusTrace = useCallback(
    async (traceId: string): Promise<boolean> => {
      if (!traceId) return false;
      if (activeTrace?.id === traceId) return true;
      setContextUsage(null);
      const context = historySeqRef.current;
      const seq = ++traceSeqRef.current;
      const isCurrent = () =>
        mountedRef.current &&
        context === historySeqRef.current &&
        seq === traceSeqRef.current;
      try {
        const trace = await chatService.getTrace(traceId);
        if (!isCurrent()) return false;
        setActiveTrace(trace);
        return true;
      } catch (err: unknown) {
        if (!isCurrent()) return false;
        console.error('Failed to load trace:', err);
        toast.error('这条回答的检索轨迹拉取失败，无法定位到证据');
        return false;
      }
    },
    [activeTrace?.id],
  );

  const sendMessage = useCallback(
    async (
      content: string,
      options?: {
        useRetrieval?: boolean;
        useInsights?: boolean;
        searchMode?: 'hybrid' | 'vector';
        contextMode?: 'standard' | 'economy';
        conversationId?: string;
      },
    ) => {
      // 允许显式指定会话：调用方刚创建出来的会话还没经由 props 回流到这里，
      // 只认 props 的话首次提问会被下面的空值守卫直接吞掉
      const convId = options?.conversationId ?? conversationId;
      if (
        !mountedRef.current ||
        !content.trim() ||
        streamingRef.current ||
        historyLoadingRef.current ||
        !convId
      )
        return;
      streamingRef.current = true;
      historySeqRef.current += 1;
      traceSeqRef.current += 1;
      setActiveTrace(null);
      setContextUsage(null);

      const userMsgId = `msg-u-${crypto.randomUUID()}`;
      const userMsg: Message = {
        id: userMsgId,
        conversation_id: convId,
        role: 'user',
        content: content.trim(),
        created_at: Date.now(),
      };

      const assistantMsgId = `msg-a-${crypto.randomUUID()}`;
      const initialAssistantMsg: Message = {
        id: assistantMsgId,
        conversation_id: convId,
        role: 'assistant',
        content: '',
        citations: [],
        created_at: Date.now(),
      };

      setMessages((prev) => [...prev, userMsg, initialAssistantMsg]);
      setIsStreaming(true);

      // 重置过程链条
      setProcessStages([
        {
          id: 'rewrite',
          label: '查询改写',
          status: 'running',
          detail: '正在优化关键词语义空间...',
        },
        { id: 'retrieval', label: '混合检索', status: 'pending' },
        { id: 'insights', label: '经验注入', status: 'pending' },
        { id: 'generating', label: '模型生成', status: 'pending' },
      ]);

      const controller = new AbortController();
      abortControllerRef.current = controller;
      streamConversationRef.current = convId;
      const seq = ++streamSeqRef.current;
      const isCurrent = () =>
        mountedRef.current &&
        seq === streamSeqRef.current &&
        abortControllerRef.current === controller &&
        !controller.signal.aborted;
      const finishCurrent = () => {
        if (!isCurrent()) return;
        streamingRef.current = false;
        abortControllerRef.current = null;
        streamConversationRef.current = null;
        setIsStreaming(false);
      };

      const writer = createStreamWriter((text) => {
        if (!isCurrent()) return;
        setMessages((prev) =>
          prev.map((msg) =>
            msg.id === assistantMsgId ? { ...msg, content: text } : msg,
          ),
        );
      });
      streamWriterRef.current = writer;
      const releaseWriter = () => {
        writer.cancel();
        if (streamWriterRef.current === writer) streamWriterRef.current = null;
      };

      if (isMockMode()) {
        try {
          await pause(450, controller.signal);
          if (!isCurrent()) return;
          setProcessStages((prev) => [
            {
              id: 'rewrite',
              label: '查询改写',
              status: 'done',
              detail: `“${content.slice(0, 18)}...” → 扩展药物化学与药理学专业术语`,
            },
            {
              id: 'retrieval',
              label: '混合检索',
              status: 'running',
              detail: '正在执行向量密集搜索与 BM25 精排...',
            },
            prev[2],
            prev[3],
          ]);
          await pause(550, controller.signal);
          if (!isCurrent()) return;
          setProcessStages((prev) => [
            prev[0],
            {
              id: 'retrieval',
              label: '混合检索',
              status: 'done',
              detail: '召回 6 条切片，Rerank 过滤前 3 条核心证据',
            },
            {
              id: 'insights',
              label: '经验注入',
              status: 'running',
              detail: '正在匹配相关经验...',
            },
            prev[3],
          ]);
          await pause(400, controller.signal);
          if (!isCurrent()) return;
          setProcessStages((prev) => [
            prev[0],
            prev[1],
            {
              id: 'insights',
              label: '经验注入',
              status: 'done',
              detail:
                '应用经验：评估先导物成药性优先考量 ADMET 五项性质 (0.96 置信度)',
            },
            {
              id: 'generating',
              label: '模型生成',
              status: 'running',
              detail: '模型流式吐字中...',
            },
          ]);

          const mockStreamText = `针对 EGFR T790M 耐药突变的小分子先导物设计，核心策略聚焦于**共价不可逆结合**与**野生型选择性窗口**：

1. **引入迈克尔加成受体形成共价键** [^c1]：在喹唑啉或嘧啶核心骨架引入丙烯酰胺弹头，与靶点 ATP 口的 Cys797 残基形成共价不可逆结合。
2. **规避野生型 EGFR 毒性窗口** [^c2]：设计柔性甲氨基或芳胺取代基与铰链区深度契合，使突变体 IC50 / WT 比值大于 80 倍。
3. **前置 ADMET 与 hERG 心脏毒性反筛** [^c3]：在纳摩尔级活性确认后立即进行微粒体代谢稳定性及 hERG 钾通道抑制测试。`;
          const citations: CitationMarker[] = [
            {
              marker: 'c1',
              chunk_id: 'chk-01',
              document_id: 'doc-01',
              document_title:
                '第三代小分子 EGFR-TKI 抑制剂设计与构效关系研究.pdf',
              snippet:
                '奥希替尼等第三代化合物通过丙烯酰胺基团与 EGFR ATP 结合口袋边缘保留的 Cys797 形成共价键结合。',
            },
            {
              marker: 'c2',
              chunk_id: 'chk-02',
              document_id: 'doc-02',
              document_title: '激酶选择性突变体筛选与脱靶毒性评估指南.md',
              snippet:
                '针对 T790M/L858R 双突变体的亲和力窗口需显著优于 WT 野生型，以减轻皮疹与腹泻脱靶毒性。',
            },
            {
              marker: 'c3',
              chunk_id: 'chk-03',
              document_id: 'doc-03',
              document_title: '药物代谢动力学与早期 ADMET 筛选规程.pdf',
              snippet:
                '在先导化合物优化早期必须评估 hERG 钾通道 IC50，避免后期因 QT 间期延长导致临床终止。',
            },
          ];

          // 对齐真实后端的流式契约：正文里的 [^cN] 在流出时剥离，走到对应位置时
          // 以 citation 事件（带 char_offset）补发。mock 若把裸标记留在正文里，
          // 引用事件到达前会渲染成灰色 [cN] 兜底、结束又跳成数字芯片——
          // 演示路径与生产路径必须是同一种长相
          const citationEvents: CitationMarker[] = [];
          let strippedText = '';
          {
            let cursor = 0;
            for (const match of mockStreamText.matchAll(/\[\^c(\d+)\]/g)) {
              strippedText += mockStreamText.slice(cursor, match.index);
              const def = citations.find((c) => c.marker === `c${match[1]}`);
              if (def) {
                citationEvents.push({ ...def, char_offset: strippedText.length });
              } else {
                strippedText += match[0];
              }
              cursor = match.index + match[0].length;
            }
            strippedText += mockStreamText.slice(cursor);
          }

          const emittedCitations: CitationMarker[] = [];
          let nextCitation = 0;
          let currentText = '';
          const chars = strippedText.split('');
          for (let i = 0; i < chars.length; i++) {
            if (!isCurrent()) return;
            currentText += chars[i];
            writer.write(currentText);
            while (
              nextCitation < citationEvents.length &&
              (citationEvents[nextCitation].char_offset as number) <=
                currentText.length
            ) {
              emittedCitations.push(citationEvents[nextCitation]);
              nextCitation += 1;
              setMessages((prev) =>
                prev.map((msg) =>
                  msg.id === assistantMsgId
                    ? { ...msg, citations: [...emittedCitations] }
                    : msg,
                ),
              );
            }
            await pause(18, controller.signal);
          }
          writer.flushNow();

          setMessages((prev) =>
            prev.map((msg) =>
              msg.id === assistantMsgId
                ? {
                    ...msg,
                    citations: [...emittedCitations],
                    trace_id: mockTraceDetail.id,
                  }
                : msg,
            ),
          );
          if (!isCurrent()) return;
          setActiveTrace({
            ...mockTraceDetail,
            space_id: spaceId || '',
            conversation_id: convId,
          });
          setProcessStages((prev) => [
            prev[0],
            prev[1],
            prev[2],
            {
              id: 'generating',
              label: '模型生成',
              status: 'done',
              detail: 'deepseek-chat · 3.0k→428 tok · 4.2s',
            },
          ]);
        } catch (err: unknown) {
          if (isCurrent() && (err as Error)?.name !== 'AbortError') {
            toast.error('流式生成出错');
          }
        } finally {
          releaseWriter();
          finishCurrent();
        }
        return;
      }

      // 走真实后端 SSE
      let accumulatedText = '';
      let receivedTerminal = false;
      const accumulatedCitations: CitationMarker[] = [];

      try {
        await chatService.streamChat(
          convId,
          content.trim(),
          {
            onContext: (data) => {
              if (isCurrent()) setContextUsage(data);
            },
            onTraceStart: (data) => {
              if (!isCurrent()) return;
              setActiveTrace({
                id: data.trace_id,
                space_id: spaceId || '',
                conversation_id: convId,
                message_id: assistantMsgId,
                created_at: Date.now(),
                query: content.trim(),
                retrieved: [],
                used_insights: [],
                used_cards: [],
              });
              setProcessStages((prev) => [
                { ...prev[0], status: 'running' },
                prev[1],
                prev[2],
                prev[3],
              ]);
            },
            onRewrite: (data) => {
              if (!isCurrent()) return;
              // 轨迹面板的「意图重写」也要落上，否则流式期间它总说「未发生查询重写」
              setActiveTrace((prev) =>
                prev ? { ...prev, rewritten_query: data.rewritten } : null,
              );
              setProcessStages((prev) => [
                {
                  id: 'rewrite',
                  label: '查询改写',
                  status: 'done',
                  detail: data.rewritten,
                },
                {
                  id: 'retrieval',
                  label: '混合检索',
                  status: 'running',
                  detail: '正在检索相关文档切片...',
                },
                prev[2],
                prev[3],
              ]);
            },
            onRetrieval: (data) => {
              if (!isCurrent()) return;
              const chunks = (data.chunks || []).map((c) => ({
                ...c,
                chunk_id: c.chunk_id || c.id || '',
                document_id: c.document_id || '',
                document_title: c.document_title || c.title || '未命名文档',
                kind: c.kind ?? 'body',
              }));
              setActiveTrace((prev) =>
                prev ? { ...prev, retrieved: chunks } : null,
              );
              setProcessStages((prev) => [
                prev[0].status === 'running'
                  ? { ...prev[0], status: 'done', detail: '无需改写' }
                  : prev[0],
                {
                  id: 'retrieval',
                  label: '混合检索',
                  status: 'done',
                  detail: `召回 ${chunks.length} 条切片证据`,
                  warning: describeDegraded(data.degraded),
                },
                {
                  id: 'insights',
                  label: '经验注入',
                  status: 'running',
                  detail: '正在匹配相关经验...',
                },
                prev[3],
              ]);
            },
            onInsights: (data) => {
              if (!isCurrent()) return;
              const insights = (data.insights || []).map((i) => ({
                ...i,
                space_id: spaceId || '',
                applied_count: 0,
                success_count: 0,
                created_at: Date.now(),
                updated_at: Date.now(),
                kind: 'heuristic' as const,
                // 作用域以后端为准，不再一律写死成「空间」
                scope: i.scope ?? ('space' as const),
                status: 'active' as const,
                origin: 'judge' as const,
              }));
              setActiveTrace((prev) =>
                prev ? { ...prev, used_insights: insights } : null,
              );
              setProcessStages((prev) => [
                prev[0],
                prev[1],
                {
                  id: 'insights',
                  label: '经验注入',
                  status: 'done',
                  detail: `注入 ${insights.length} 条置信经验`,
                },
                {
                  id: 'generating',
                  label: '模型生成',
                  status: 'running',
                  detail: '流式吐字中...',
                },
              ]);
            },
            onDelta: (data) => {
              if (!isCurrent()) return;
              accumulatedText += data.text || '';
              writer.write(accumulatedText);
            },
            onCitation: (data) => {
              if (!isCurrent()) return;
              const marker: CitationMarker = {
                // 原文定位（章节、段序、字符区间）原样带上，界面据此标出处
                ...data,
                marker: data.marker,
                chunk_id: data.chunk_id,
                document_id: data.document_id,
                document_title: data.document_title || data.document_id,
                page: data.page,
                snippet: data.snippet,
                // 后端在 citation 事件里带 char_offset（标记被剥离处的绝对下标）。
                // 丢掉它的话流式期间 `withCitationMarkers` 插不回正文，inline 引用芯片
                // 要刷新页面读历史才出现——同一条回答流式与历史两种长相
                char_offset: data.char_offset ?? null,
              };
              accumulatedCitations.push(marker);
              setMessages((prev) =>
                prev.map((msg) =>
                  msg.id === assistantMsgId
                    ? { ...msg, citations: [...accumulatedCitations] }
                    : msg,
                ),
              );
            },
            onDone: (data) => {
              if (!isCurrent()) return;
              receivedTerminal = true;
              writer.flushNow();
              // 用量与耗时都在 usage 里（DonePayload 的契约），顶层的扁平字段是
              // 早期 mock 的形状，已经去掉
              const usage = data.usage;
              const promptTokens = usage?.prompt_tokens ?? 0;
              const completionTokens = usage?.completion_tokens ?? 0;
              const latencyMs = usage?.latency_ms ?? 0;
              setMessages((prev) =>
                prev.map((msg) =>
                  msg.id === assistantMsgId
                    ? { ...msg, trace_id: data.trace_id }
                    : msg,
                ),
              );
              // 轨迹面板要显示真实 token，两个字段都得落上去：
              // 后端把用量装在 done.usage 里，不是顶层的 total_tokens。
              setActiveTrace((prev) =>
                prev
                  ? {
                      ...prev,
                      id: data.trace_id,
                      prompt_tokens: promptTokens,
                      completion_tokens: completionTokens,
                      latency_ms: latencyMs,
                    }
                  : null,
              );
              setProcessStages((prev) => [
                prev[0],
                prev[1],
                prev[2],
                {
                  id: 'generating',
                  label: '模型生成',
                  status: 'done',
                  detail: `${promptTokens + completionTokens} tokens · ${(latencyMs / 1000).toFixed(1)}s`,
                },
              ]);
              finishCurrent();
            },
            onError: (err) => {
              if (!isCurrent()) return;
              receivedTerminal = true;
              writer.flushNow();
              const reason = err.message || '推理生成失败';
              // 助手气泡不能一直停在「思考中…」：把失败原因写进正文，
              // 否则用户看到的是一条永远转不完的消息
              setMessages((prev) =>
                prev.map((msg) =>
                  msg.id === assistantMsgId
                    ? { ...msg, content: `回答失败：${reason}` }
                    : msg,
                ),
              );
              setProcessStages((prev) =>
                prev.map((s) =>
                  s.status === 'running'
                    ? { ...s, status: 'failed', detail: reason }
                    : s,
                ),
              );
              toast.error(reason);
              finishCurrent();
            },
          },
          controller.signal,
          {
            useRetrieval: options?.useRetrieval ?? true,
            searchMode: options?.searchMode ?? 'hybrid',
            useInsights: options?.useInsights ?? true,
            contextMode: options?.contextMode ?? 'standard',
          },
        );
        if (isCurrent() && !receivedTerminal) {
          writer.flushNow();
          const reason = '连接已结束，未收到完整回答，请重试。';
          setMessages((previous) =>
            previous.map((message) =>
              message.id === assistantMsgId
                ? {
                    ...message,
                    content: `${message.content}${message.content ? '\n\n' : ''}> 回答未完成：${reason}`,
                  }
                : message,
            ),
          );
          setProcessStages((previous) =>
            previous.map((stage) =>
              stage.status === 'running'
                ? { ...stage, status: 'failed', detail: reason }
                : stage,
            ),
          );
          toast.error(reason);
        }
      } catch (err: unknown) {
        if (!isCurrent()) return;
        if ((err as Error)?.name !== 'AbortError') {
          // 请求根本没发成功（例如 422 / 503）：把助手气泡里的「思考中…」换成错误说明，
          // 只弹一个 toast 的话用户看到的是一条永远转不完的消息
          writer.flushNow();
          const reason = (err as Error)?.message || '流式连接中断';
          setMessages((prev) =>
            prev.map((msg) =>
              msg.id === assistantMsgId
                ? { ...msg, content: `请求失败：${reason}` }
                : msg,
            ),
          );
          setProcessStages((prev) =>
            prev.map((stage) =>
              stage.status === 'running'
                ? { ...stage, status: 'failed', detail: reason }
                : stage,
            ),
          );
          toast.error(reason);
        }
      } finally {
        releaseWriter();
        finishCurrent();
      }
    },
    [conversationId, spaceId],
  );

  const abortStream = useCallback(async () => {
    const activeConversation = streamConversationRef.current;
    if (!activeConversation) return;
    // 先把缓冲里的最后一段回答落地，再作废弃流——停止时不能丢掉最后几十毫秒的文本
    streamWriterRef.current?.flushNow();
    invalidateStream();
    setIsStreaming(false);
    setProcessStages((previous) =>
      previous.map((stage) =>
        stage.status === 'running'
          ? { ...stage, status: 'failed', detail: '用户主动中止' }
          : stage,
      ),
    );
    toast.info('已中止生成');
    if (!isMockMode()) {
      try {
        await chatService.stopChat(activeConversation);
      } catch {
        /* 本地流已中止。 */
      }
    }
  }, [invalidateStream]);

  return {
    messages,
    setMessages,
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
  };
}

/**
 * 把检索降级翻译成人话。
 *
 * 本地模型全挂时检索会退化成纯全文、没有重排，问答照常出结果——此前界面上什么都
 * 看不出来，用户拿到更差的回答却无从知晓（实测本机 sentence-transformers 被误删后，
 * 告警里 42/42 次失败，而对话页一切如常）。
 */
export function describeDegraded(degraded?: string[]): string | undefined {
  if (!degraded || degraded.length === 0) return undefined;
  const parts: string[] = [];
  if (degraded.includes('vector')) parts.push('向量检索没用上，只按关键词匹配');
  if (degraded.includes('rerank')) parts.push('重排没用上，证据按粗排顺序');
  return parts.length > 0 ? `本轮检索打了折扣：${parts.join('；')}` : undefined;
}
