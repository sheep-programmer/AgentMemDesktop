import React, { useEffect, useMemo, useState, useRef } from 'react';
import type { Message, ProcessStage, TraceDetail } from '@/lib/api/types.temp';
import { useUiStore } from '@/stores/useUiStore';
import { withCitationMarkers } from '@/lib/markdown';
import { numberCitations, describeLocation, quoteRangeOf } from '../lib/citations';
import { RetrievalBlock } from './RetrievalBlock';
import { MarkdownView } from '@/components/shared/MarkdownView';
import { Button } from '@/components/ui/button';
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/ui/popover';
import {
  Copy,
  Check,
  RotateCw,
  ThumbsUp,
  ThumbsDown,
  Edit3,
  Sparkles,
  FileText,
  BookOpen,
  Loader2,
} from 'lucide-react';
import { toast } from 'sonner';
import { chatService } from '@/lib/api/services/chat';

interface TraceFeedbackState {
  hasUp?: boolean;
  hasDown?: boolean;
  correctionCount: number;
}

/**
 * 反馈状态以后端为准：会话详情里每条回答带着它收到过的反馈种类。
 * 此前只记在 sessionStorage，刷新、换窗口就忘了点过赞，用户以为没提交上。
 */
function feedbackStateFrom(
  kinds?: readonly string[] | null,
): TraceFeedbackState {
  const list = kinds ?? [];
  return {
    hasUp: list.includes('up'),
    hasDown: list.includes('down'),
    correctionCount: list.filter(
      (kind) => kind === 'correction' || kind === 'edit',
    ).length,
  };
}

interface ChatMessageItemProps {
  message: Message;
  isStreaming?: boolean;
  /** 把证据栏切到这条回答的轨迹上；返回是否拿到了轨迹。 */
  onFocusTrace?: (traceId: string) => Promise<boolean>;
  /** 用同一个问题再问一次。缺省时不显示「重新生成」——
   *  这个按钮以前只弹一句「触发重新生成」，实际什么都不做。 */
  onRegenerate?: (assistantMessageId: string) => void;
  /** 只有正在生成的那条回答拿得到：实时阶段与流式轨迹，用于回答上方的检索块。 */
  liveStages?: ProcessStage[];
  liveTrace?: TraceDetail | null;
}

/**
 * memo 化是有实测依据的：流式回答时消息列表每几十毫秒重渲染一次，
 * 不 memo 的话每条已完成的 Markdown 气泡（表格、代码高亮）都会跟着整棵重算。
 * 调用方要保证 onRegenerate 的身份稳定（用 ref 包一层），否则 memo 失效。
 */
export const ChatMessageItem = React.memo(function ChatMessageItem({
  message,
  isStreaming = false,
  onFocusTrace,
  onRegenerate,
  liveStages,
  liveTrace,
}: ChatMessageItemProps) {
  const isUser = message.role === 'user';
  const {
    setEvidenceOpen,
    setActiveEvidenceTab,
    setHighlightedChunkId,
    openReader,
  } = useUiStore();
  const [copied, setCopied] = useState(false);
  const copyTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (copyTimer.current) clearTimeout(copyTimer.current);
    },
    [],
  );
  const [dislikeReason, setDislikeReason] = useState('');
  const [isDislikeOpen, setIsDislikeOpen] = useState(false);
  const [isCorrectionOpen, setIsCorrectionOpen] = useState(false);
  const [correctedText, setCorrectedText] = useState('');
  const [submittingKind, setSubmittingKind] = useState<
    'up' | 'down' | 'correction' | null
  >(null);

  const [feedbackState, setFeedbackState] = useState<TraceFeedbackState>(() =>
    feedbackStateFrom(message.feedback),
  );
  // 历史重新拉回来（刷新、切回这个会话）时，以后端记录为准
  const feedbackKey = (message.feedback ?? []).join(',');
  useEffect(() => {
    setFeedbackState(
      feedbackStateFrom(feedbackKey ? feedbackKey.split(',') : []),
    );
  }, [message.id, feedbackKey]);

  const hasTrace = Boolean(message.trace_id);

  // 角标按正文里出现的先后编成 1、2、3…，同一切片共用一个号（见 lib/citations）
  const numbering = useMemo(
    () => numberCitations(message.citations),
    [message.citations],
  );
  const displayCitations = useMemo(
    () => numbering.items.map((item) => ({ ...item, marker: `c${item.n}` })),
    [numbering],
  );
  const markedContent = useMemo(
    () =>
      withCitationMarkers(
        message.content,
        (message.citations ?? []).map((citation) => ({
          marker: `c${numbering.byChunk.get(citation.chunk_id || citation.marker) ?? 0}`,
          char_offset: citation.char_offset,
        })),
      ),
    [message.content, message.citations, numbering],
  );

  const openInReader = (target: {
    document_id?: string | null;
    document_title?: string | null;
    chunk_id: string;
    kind?: string | null;
    quote_start?: number | null;
    quote_end?: number | null;
  }) => {
    if (!target.document_id) {
      void handleCitationClick(target.chunk_id);
      return;
    }
    openReader({
      documentId: target.document_id,
      documentTitle: target.document_title || '文档',
      chunkId: target.kind === 'summary' ? undefined : target.chunk_id,
      quote: quoteRangeOf(target),
    });
  };

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(message.content);
      setCopied(true);
      toast.success('已复制到剪贴板');
      if (copyTimer.current) clearTimeout(copyTimer.current);
      copyTimer.current = setTimeout(() => setCopied(false), 2000);
    } catch {
      toast.error('复制未成功，请选中回答文字后手动复制');
    }
  };

  // 点引用 → 打开证据栏并高亮那条切片。
  //
  // 先把证据栏切到**这条回答**的轨迹上：它一次只装得下一份轨迹，不切的话，点历史
  // 回答里的引用会落在最后一次提问的证据列表上——那里没有这个切片，看起来就是
  // 「点了没反应」。
  const handleCitationClick = async (chunkId: string) => {
    setEvidenceOpen(true);
    setActiveEvidenceTab('chunks');
    if (message.trace_id && onFocusTrace) {
      await onFocusTrace(message.trace_id);
    }
    setHighlightedChunkId(chunkId);
  };

  const handleThumbsUp = async () => {
    if (!message.trace_id) {
      toast.error('当前回答未关联执行轨迹，无法提交反馈');
      return;
    }
    if (feedbackState.hasUp) {
      toast.info('已提交过正向反馈，无需重复提交');
      return;
    }

    setSubmittingKind('up');
    try {
      await chatService.sendFeedback(message.trace_id, { kind: 'up' });
      const nextState: TraceFeedbackState = { ...feedbackState, hasUp: true };
      setFeedbackState(nextState);
      toast.success('已记录：这条回答对你有帮助');
    } catch (err: unknown) {
      toast.error((err as Error)?.message || '正向反馈提交失败，请重试');
    } finally {
      setSubmittingKind(null);
    }
  };

  const handleThumbsDown = async () => {
    if (!message.trace_id) {
      toast.error('当前回答未关联执行轨迹，无法提交反馈');
      return;
    }
    if (feedbackState.hasDown) {
      toast.info('已提交过负向反馈，无需重复提交');
      return;
    }

    setSubmittingKind('down');
    try {
      await chatService.sendFeedback(message.trace_id, {
        kind: 'down',
        comment: dislikeReason.trim() || undefined,
      });
      const nextState: TraceFeedbackState = { ...feedbackState, hasDown: true };
      setFeedbackState(nextState);
      toast.success('已记录反馈，下次进化时会参考');
      setIsDislikeOpen(false);
      setDislikeReason('');
    } catch (err: unknown) {
      toast.error((err as Error)?.message || '负向反馈提交失败，请重试');
    } finally {
      setSubmittingKind(null);
    }
  };

  const submitCorrection = async () => {
    if (!message.trace_id) {
      toast.error('当前回答未关联执行轨迹，无法提交纠偏');
      return;
    }
    if (!correctedText.trim()) {
      toast.error('请输入纠偏正文内容');
      return;
    }

    setSubmittingKind('correction');
    try {
      await chatService.sendFeedback(message.trace_id, {
        kind: 'correction',
        comment: correctedText.trim(),
      });
      const isAdditional = (feedbackState.correctionCount || 0) > 0;
      const nextState: TraceFeedbackState = {
        ...feedbackState,
        correctionCount: (feedbackState.correctionCount || 0) + 1,
      };
      setFeedbackState(nextState);

      if (isAdditional) {
        toast.success('已追加纠错，将作为待学习素材');
      } else {
        toast.success('纠错已保存，将作为待学习素材');
      }
      setIsCorrectionOpen(false);
      setCorrectedText('');
    } catch (err: unknown) {
      toast.error((err as Error)?.message || '纠偏提交失败，请重试');
    } finally {
      setSubmittingKind(null);
    }
  };

  if (isUser) {
    return (
      <div className="flex w-full justify-end pb-1 pt-5">
        <div className="max-w-[80%] whitespace-pre-wrap break-words rounded-[18px] border border-border/60 bg-muted px-4 py-2.5 text-[14.5px] leading-relaxed text-foreground">
          {message.content}
        </div>
      </div>
    );
  }

  return (
    <div className="group/message relative flex w-full justify-start py-3">
      <div className="flex w-full items-start gap-3">
        <div
          aria-hidden="true"
          className="mt-1 flex h-6 w-6 shrink-0 items-center justify-center rounded-lg bg-primary text-primary-foreground"
        >
          <Sparkles className="h-3.5 w-3.5" />
        </div>

        <div className="flex-1 min-w-0 max-w-[74ch] space-y-3">
          <RetrievalBlock
            numbering={numbering}
            traceId={message.trace_id}
            liveStages={liveStages}
            liveTrace={liveTrace}
            isStreaming={isStreaming}
            onLocate={(chunkId) => void handleCitationClick(chunkId)}
            onOpen={openInReader}
          />
          {/* 消息正文与流式光标，支持内联 [^c1] 芯片 */}
          <div
            className={`answer-prose prose prose-sm dark:prose-invert max-w-none text-foreground text-[14.5px] leading-[1.75] [&_p]:leading-[1.75] [&_p]:mb-3.5 [&_p:last-child]:mb-0 [&_ul]:my-2.5 [&_ol]:my-2.5 [&_li]:leading-[1.75] ${isStreaming ? 'streaming-cursor' : ''}`}
          >
            <MarkdownView
              citations={displayCitations}
              onCitationClick={handleCitationClick}
            >
              {/* 空正文有两种成因，不能都说成「思考中」：真在流式生成时才是思考中，
                  否则就是这次生成失败了（provider 失联、被中断）留下的空壳——
                  刷新后它会永远挂着一个假的思考态，让人以为还在跑 */}
              {markedContent ||
                (isStreaming
                  ? '思考中...'
                  : '_本次生成未返回内容（模型调用失败或已中断），可重新提问。_')}
            </MarkdownView>
          </div>

          {/* 来源：按角标序号排的卡片，每张写明出自哪篇、哪一节、第几段 */}
          {!isStreaming && numbering.items.length > 0 && (
            <div className="pt-1">
              <div className="mb-1.5 flex items-center gap-1.5 text-[11.5px] font-medium text-muted-foreground">
                <BookOpen className="h-3.5 w-3.5" />
                来源 · {numbering.items.length}
              </div>
              <ul className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
                {numbering.items.map((c) => {
                  const location = describeLocation(c);
                  return (
                    <li key={c.chunk_id || c.marker}>
                      <Popover>
                        <PopoverTrigger
                          onClick={() => void handleCitationClick(c.chunk_id)}
                          title={`引用 ${c.n}：${c.document_title || '来源文档'}${location ? ` · ${location}` : ''}`}
                          className="group flex w-full items-start gap-2 rounded-lg border border-border/80 bg-card px-2.5 py-2 text-left shadow-[var(--shadow-card)] transition-colors hover:border-primary/40 cursor-pointer"
                        >
                          <span className="mt-px flex h-[18px] min-w-[18px] shrink-0 items-center justify-center rounded-[5px] bg-primary/10 px-1 font-mono text-[10.5px] font-semibold text-primary">
                            {c.n}
                          </span>
                          <span className="min-w-0 flex-1">
                            <span className="block truncate text-[12.5px] font-medium text-foreground">
                              {c.document_title || '未命名资料'}
                            </span>
                            <span className="mt-0.5 block truncate text-[11.5px] text-muted-foreground">
                              {location || '点击查看原文片段'}
                            </span>
                            {c.quote && (
                              <span className="mt-1 line-clamp-2 text-[11.5px] leading-snug text-foreground/70">
                                “{c.quote}”
                              </span>
                            )}
                          </span>
                        </PopoverTrigger>
                        <PopoverContent className="w-[22rem] max-w-[calc(100vw_-_1.5rem)] p-0 text-xs">
                          <div className="flex items-start gap-2 border-b border-border/60 px-3 py-2.5">
                            <FileText className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" />
                            <div className="min-w-0">
                              <div className="truncate font-semibold text-foreground">
                                {c.document_title || '证据详情'}
                              </div>
                              {location && (
                                <div className="mt-0.5 text-[11.5px] text-muted-foreground">
                                  {location}
                                </div>
                              )}
                            </div>
                          </div>
                          {c.quote && (
                            <div className="border-b border-border/60 bg-primary/[0.04] px-3 py-2.5">
                              <div className="mb-1 text-[10.5px] font-medium tracking-wide text-primary">
                                依据原句
                              </div>
                              <p className="text-[12.5px] leading-relaxed text-foreground">
                                {c.quote}
                              </p>
                            </div>
                          )}
                          {/* 证据片段就是原文切片，里面常带表格、列表、代码块；
                              按纯文本显示会把 Markdown 记号原样漏出来（表格会变成一串竖线） */}
                          <div className="max-h-56 overflow-y-auto px-3 py-2.5 text-[12px] leading-relaxed text-foreground/85 prose prose-sm dark:prose-invert max-w-none prose-p:my-1 prose-table:my-1 prose-pre:my-1">
                            <MarkdownView>
                              {c.snippet || '（该切片没有可展示的片段）'}
                            </MarkdownView>
                          </div>
                          <div className="flex items-center justify-between gap-2 border-t border-border/60 px-3 py-2">
                            <button
                              type="button"
                              onClick={() => void handleCitationClick(c.chunk_id)}
                              className="text-[11.5px] text-muted-foreground hover:text-foreground cursor-pointer"
                            >
                              在证据栏定位
                            </button>
                            <button
                              type="button"
                              onClick={() => openInReader(c)}
                              className="rounded-md bg-primary px-2 py-1 text-[11.5px] font-medium text-primary-foreground hover:bg-primary/90 cursor-pointer"
                            >
                              {c.kind === 'summary' ? '打开全文' : '打开原文并高亮'}
                            </button>
                          </div>
                        </PopoverContent>
                      </Popover>
                    </li>
                  );
                })}
              </ul>
            </div>
          )}

          {/* Actions remain discoverable with keyboard, mouse and touch. */}
          {!isStreaming && message.content && (
            <div
              role="group"
              aria-label="回答操作"
              className="-ml-2 flex flex-wrap items-center gap-0.5 pt-0.5 text-muted-foreground"
            >
              <Button
                variant="ghost"
                size="sm"
                className="h-8 gap-1.5 px-2 text-xs"
                onClick={handleCopy}
                title="复制内容"
              >
                {copied ? (
                  <Check className="h-3.5 w-3.5 text-accent-insight" />
                ) : (
                  <Copy className="h-3.5 w-3.5" />
                )}
                {copied ? '已复制' : '复制'}
              </Button>
              {onRegenerate && (
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-8 gap-1.5 px-2 text-xs"
                  onClick={() => onRegenerate(message.id)}
                  title="重新生成（用同一个问题再问一次）"
                >
                  <RotateCw className="h-3.5 w-3.5" />
                  再答一次
                </Button>
              )}

              {/* 点赞反馈 */}
              <Button
                variant="ghost"
                size="sm"
                aria-pressed={Boolean(feedbackState.hasUp)}
                disabled={
                  !hasTrace || feedbackState.hasUp || submittingKind !== null
                }
                className={
                  feedbackState.hasUp
                    ? 'h-8 gap-1.5 px-2 text-xs rounded-md text-primary bg-primary/10'
                    : 'h-9 gap-1.5 px-2 text-xs'
                }
                title={
                  !hasTrace
                    ? '当前回答未关联执行轨迹，无法评价'
                    : feedbackState.hasUp
                      ? '已提交正向反馈（已提升经验置信度）'
                      : '满意（提升经验置信度）'
                }
                onClick={handleThumbsUp}
              >
                {submittingKind === 'up' ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin text-primary" />
                ) : (
                  <ThumbsUp className="h-3.5 w-3.5" />
                )}
                {feedbackState.hasUp ? '已认可' : '有帮助'}
              </Button>

              {/* 踩反馈 */}
              <Popover open={isDislikeOpen} onOpenChange={setIsDislikeOpen}>
                <PopoverTrigger
                  aria-label={
                    feedbackState.hasDown ? '已提交不满意反馈' : '不满意'
                  }
                  disabled={
                    !hasTrace ||
                    feedbackState.hasDown ||
                    submittingKind !== null
                  }
                  className={
                    feedbackState.hasDown
                      ? 'h-8 flex items-center gap-1.5 px-2 rounded-md text-xs text-destructive bg-destructive/10'
                      : 'flex h-8 items-center gap-1.5 rounded-md px-2 text-xs hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:opacity-50'
                  }
                  title={
                    !hasTrace
                      ? '当前回答未关联执行轨迹，无法评价'
                      : feedbackState.hasDown
                        ? '已提交负向反馈（已扣减经验置信度）'
                        : '提出问题（扣减经验置信度）'
                  }
                >
                  {submittingKind === 'down' ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin text-destructive" />
                  ) : (
                    <ThumbsDown className="h-3.5 w-3.5" />
                  )}
                  {feedbackState.hasDown ? '已反馈' : '不满意'}
                </PopoverTrigger>
                <PopoverContent className="w-72 max-w-[calc(100vw_-_1.5rem)] p-4">
                  <div className="text-xs font-medium text-foreground">
                    哪里回答得不够好？
                  </div>
                  <p className="mt-1 text-[11px] text-muted-foreground leading-relaxed">
                    告诉我们遗漏或不准确的地方，系统会在后续进化时参考。
                  </p>
                  <input
                    type="text"
                    aria-label="不满意的原因（可选）"
                    value={dislikeReason}
                    onChange={(e) => setDislikeReason(e.target.value)}
                    placeholder="可选：遗漏了某些考量或推理有误"
                    className="mt-2 w-full rounded border border-input bg-background px-2 py-1 text-xs outline-none focus:ring-1 focus:ring-primary"
                  />
                  <div className="mt-2 flex justify-end gap-1.5">
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-7 text-xs"
                      onClick={() => setIsDislikeOpen(false)}
                    >
                      取消
                    </Button>
                    <Button
                      size="sm"
                      disabled={submittingKind === 'down'}
                      className="h-7 text-xs"
                      onClick={handleThumbsDown}
                    >
                      {submittingKind === 'down' ? (
                        <>
                          <Loader2 className="h-3 w-3 animate-spin mr-1" />
                          提交中...
                        </>
                      ) : (
                        '提交反馈'
                      )}
                    </Button>
                  </div>
                </PopoverContent>
              </Popover>

              {/* 纠错 */}
              <Popover
                open={isCorrectionOpen}
                onOpenChange={setIsCorrectionOpen}
              >
                <PopoverTrigger
                  aria-label="纠错"
                  disabled={!hasTrace || submittingKind !== null}
                  className={
                    feedbackState.correctionCount > 0
                      ? 'h-8 flex items-center gap-1.5 px-2 rounded-md text-xs text-accent-insight bg-accent-insight/10'
                      : 'flex h-8 items-center gap-1.5 rounded-md px-2 text-xs hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:opacity-50'
                  }
                  title={
                    !hasTrace
                      ? '当前回答未关联执行轨迹，无法纠偏'
                      : feedbackState.correctionCount > 0
                        ? `已提交 ${feedbackState.correctionCount} 次纠错，可以继续补充`
                        : '提供正确的事实或做法'
                  }
                >
                  {submittingKind === 'correction' ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin text-accent-insight" />
                  ) : (
                    <Edit3 className="h-3.5 w-3.5" />
                  )}
                  纠错
                  {feedbackState.correctionCount > 0
                    ? ` (${feedbackState.correctionCount})`
                    : ''}
                </PopoverTrigger>
                <PopoverContent className="w-80 max-w-[calc(100vw_-_1.5rem)] p-4">
                  <div className="flex items-center gap-1.5 text-xs font-medium text-accent-insight">
                    <Sparkles className="h-3.5 w-3.5" />
                    <span>
                      {feedbackState.correctionCount > 0
                        ? `继续纠正这条回答（已提交 ${feedbackState.correctionCount} 次）`
                        : '这条回答应该怎么改？'}
                    </span>
                  </div>
                  <p className="mt-1 text-[11px] text-muted-foreground leading-relaxed">
                    写下正确的事实或做法。纠错会加入待学习素材，经过验证的经验才会在后续回答中生效。
                  </p>
                  <textarea
                    rows={3}
                    aria-label="正确的事实或做法"
                    value={correctedText}
                    onChange={(e) => setCorrectedText(e.target.value)}
                    placeholder="请输入正确的事实或推理结论..."
                    className="mt-2 w-full rounded border border-input bg-background p-2 text-xs outline-none focus:ring-1 focus:ring-accent-insight"
                  />
                  <div className="mt-2 flex justify-end gap-1.5">
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-7 text-xs"
                      onClick={() => setIsCorrectionOpen(false)}
                    >
                      取消
                    </Button>
                    <Button
                      size="sm"
                      disabled={
                        submittingKind === 'correction' || !correctedText.trim()
                      }
                      className="h-7 text-xs bg-accent-insight hover:bg-accent-insight/90 text-background"
                      onClick={submitCorrection}
                    >
                      {submittingKind === 'correction' ? (
                        <>
                          <Loader2 className="h-3 w-3 animate-spin mr-1" />
                          提交中...
                        </>
                      ) : (
                        '保存纠错'
                      )}
                    </Button>
                  </div>
                </PopoverContent>
              </Popover>
            </div>
          )}
        </div>
      </div>
    </div>
  );
});
