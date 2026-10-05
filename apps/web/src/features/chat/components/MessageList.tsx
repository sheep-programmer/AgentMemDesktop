import { useEffect, useRef } from 'react';
import { Virtuoso, type VirtuosoHandle } from 'react-virtuoso';
import { History } from 'lucide-react';

import { Button } from '@/components/ui/button';
import type { Message, ProcessStage, TraceDetail } from '@/lib/api/types.temp';

import { ChatMessageItem } from './ChatMessageItem';

/**
 * 虚拟滚动的消息列表。
 *
 * 一条回答就是一整块 Markdown（表格、公式、代码高亮都在里面），几百条同时挂在 DOM 上
 * 会让长会话滚起来发粘。这里只渲染视口附近的那些。
 *
 * 三件容易做坏的事，都交给 Virtuoso 的既定机制，而不是自己算 scrollTop：
 *
 * - **往前加载历史不能跳位**：``firstItemIndex`` 递减时它自己补偿高度，视口停在原处；
 * - **流式回答要跟着走**：``followOutput`` 只在用户本来就贴着底部时才跟，用户往回翻
 *   看旧消息时不该被硬拽回去；
 * - **切换会话要落在最新一条**：``initialTopMostItemIndex`` 指向末尾。
 */
export interface MessageListProps {
  messages: Message[];
  /** 当前渲染的窗口起点（messages 里的下标）。 */
  startIndex: number;
  /** 还没载入的更早消息条数。 */
  remainingCount: number;
  isStreaming: boolean;
  conversationId: string | null;
  onLoadEarlier: () => void;
  onFocusTrace?: (traceId: string) => Promise<boolean>;
  onRegenerate?: (assistantMessageId: string) => void;
  /** 流式阶段：只交给正在生成的那条回答，其余条目的 props 不变、memo 不失效。 */
  liveStages?: ProcessStage[];
  /** 当前聚焦的轨迹：交给它对应的那条回答，展开检索块时不必再请求一次。 */
  activeTrace?: TraceDetail | null;
}

/** Virtuoso 要求 firstItemIndex 单调不增，所以给一个足够大的起点。 */
const INDEX_BASE = 1_000_000;

export function MessageList({
  messages,
  startIndex,
  remainingCount,
  isStreaming,
  conversationId,
  onLoadEarlier,
  onFocusTrace,
  onRegenerate,
  liveStages,
  activeTrace,
}: MessageListProps) {
  const ref = useRef<VirtuosoHandle>(null);
  const landedConvRef = useRef<string | null>(null);
  /** 落位完成前不响应「滚到顶」：落位过程本身会经过顶部，会误触发翻页。 */
  const landingRef = useRef(false);
  const visible = messages.slice(startIndex);

  // 换会话后落到最新一条。
  //
  // `initialTopMostItemIndex` 只在挂载时生效，而这个组件在会话之间是复用的；历史消息
  // 又是异步到的，所以要等到「这个会话的消息真的有了」才滚。每个会话只做一次：
  // 之后的消息增减交给 followOutput，否则用户往回翻历史会被一路拽回底部。
  useEffect(() => {
    if (visible.length === 0 || landedConvRef.current === conversationId) return;
    landedConvRef.current = conversationId;
    landingRef.current = true;

    // 滚两次是必要的：条目高度不一（一条回答可能是一张表格，也可能是一行字），
    // 第一次滚的时候 Virtuoso 用的还是估算高度，量准之后位置会漂；量完再滚一次才真到底。
    const land = () => ref.current?.scrollToIndex({ index: visible.length - 1, align: 'end' });
    land();
    const again = window.setTimeout(() => {
      land();
      landingRef.current = false;
    }, 250);
    return () => window.clearTimeout(again);
  }, [conversationId, visible.length]);

  return (
    <Virtuoso
      ref={ref}
      // 必须给定高度：Virtuoso 自己量不出 flex 容器的高度，只给 flex-1 的话它会以为
      // 视口高度接近 0，于是只渲染一条
      style={{ height: '100%' }}
      data={visible}
      firstItemIndex={INDEX_BASE - startIndex}
      initialTopMostItemIndex={Math.max(0, visible.length - 1)}
      alignToBottom
      followOutput={(isAtBottom) => (isAtBottom ? 'smooth' : false)}
      atTopStateChange={(atTop) => {
        // 滚到顶且还有更早的消息时自动续上一页，省得用户去够那个按钮；
        // 按钮本身留着，它同时是「还有多少条」的说明。
        // 落位期间不算：那一下是代码在滚，不是用户想看更早的。
        if (atTop && remainingCount > 0 && !landingRef.current) onLoadEarlier();
      }}
      components={{
        Header: () =>
          remainingCount > 0 ? (
            <div className="flex justify-center py-2">
              <Button
                variant="outline"
                size="sm"
                onClick={onLoadEarlier}
                className="gap-1.5 rounded-full border-dashed border-border/80 bg-card/60 px-3.5 py-1 text-xs text-muted-foreground hover:bg-muted/80 hover:text-foreground cursor-pointer shadow-2xs transition-all"
              >
                <History className="h-3.5 w-3.5" />
                <span>载入更早的消息（还有 {remainingCount} 条）</span>
              </Button>
            </div>
          ) : null,
      }}
      itemContent={(_index, message) => {
        const streamingThis = isStreaming && message.id === visible[visible.length - 1]?.id;
        const traceOfThis =
          activeTrace &&
          message.role === 'assistant' &&
          ((activeTrace.id && message.trace_id === activeTrace.id) ||
            activeTrace.message_id === message.id)
            ? activeTrace
            : undefined;
        return (
          <div className="mx-auto max-w-[52rem] px-4 md:px-8">
            <ChatMessageItem
              message={message}
              isStreaming={streamingThis}
              onFocusTrace={onFocusTrace}
              onRegenerate={onRegenerate}
              liveStages={streamingThis ? liveStages : undefined}
              liveTrace={traceOfThis}
            />
          </div>
        );
      }}
    />
  );
}
