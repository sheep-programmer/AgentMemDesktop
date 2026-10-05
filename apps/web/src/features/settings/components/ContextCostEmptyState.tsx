import React from 'react';
import { useNavigate } from 'react-router';
import {
  Layers,
  Sparkles,
  Zap,
  ShieldCheck,
  MessageSquare,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { useSpaceStore } from '@/stores/useSpaceStore';

export function ContextCostEmptyState() {
  const navigate = useNavigate();
  const { currentSpaceId } = useSpaceStore();

  const handleGoChat = () => {
    if (currentSpaceId) {
      navigate(`/s/${currentSpaceId}/chat`);
    } else {
      navigate('/');
    }
  };

  return (
    <div className="relative flex flex-col items-center justify-center rounded-xl border border-dashed border-border/80 bg-card/30 py-10 px-6 sm:px-10 text-center overflow-hidden">
      {/* 柔和背景光晕 */}
      <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
        <div className="h-40 w-40 rounded-full bg-accent-ai/5 blur-3xl" />
        <div className="h-28 w-28 rounded-full bg-primary/5 blur-2xl -translate-y-4 translate-x-4" />
      </div>

      {/* 复合插画感图标 */}
      <div className="relative mb-4 flex items-center justify-center">
        <div className="flex h-14 w-14 items-center justify-center rounded-2xl border border-border/80 bg-card/90 text-accent-ai shadow-xs backdrop-blur-xs">
          <Layers className="h-7 w-7 text-accent-ai" />
        </div>
        <div className="absolute -top-1.5 -right-1.5 flex h-6 w-6 items-center justify-center rounded-full border border-accent-insight/40 bg-accent-insight/15 text-accent-insight shadow-2xs backdrop-blur-xs">
          <Sparkles className="h-3 w-3" />
        </div>
      </div>

      {/* 标题与主文案 */}
      <h4 className="text-sm font-semibold text-foreground tracking-tight">
        暂无上下文用量记录
      </h4>
      <p className="mt-1.5 max-w-md text-xs text-muted-foreground leading-relaxed">
        当前时间范围还没有模型用量。完成一次问答后，可以回来查看输入、输出和缓存记录。
      </p>

      {/* 主行动按钮 (符合规范 §2.3) */}
      <div className="mt-4 flex items-center justify-center">
        <Button
          variant="outline"
          size="sm"
          onClick={handleGoChat}
          className="gap-1.5 text-xs text-foreground border-border/80 hover:border-accent-ai/50 hover:text-accent-ai transition-colors"
        >
          <MessageSquare className="h-3.5 w-3.5" />
          去提问
        </Button>
      </div>

      {/* 机制说明网格 */}
      <div className="mt-6 grid grid-cols-1 sm:grid-cols-3 gap-3 w-full max-w-xl text-left">
        <div className="rounded-lg border border-border/60 bg-muted/20 p-3">
          <div className="flex items-center gap-1.5 text-xs font-medium text-foreground">
            <Layers className="h-3.5 w-3.5 text-accent-ai" />
            输入量
          </div>
          <p className="mt-1 text-[11px] text-muted-foreground leading-normal">
            问题、历史和资料都会计入模型收到的内容。
          </p>
        </div>

        <div className="rounded-lg border border-border/60 bg-muted/20 p-3">
          <div className="flex items-center gap-1.5 text-xs font-medium text-foreground">
            <Zap className="h-3.5 w-3.5 text-accent-ai" />
            缓存复用
          </div>
          <p className="mt-1 text-[11px] text-muted-foreground leading-normal">
            相同的输入前缀可能被复用，命中量由服务商报告。
          </p>
        </div>

        <div className="rounded-lg border border-border/60 bg-muted/20 p-3">
          <div className="flex items-center gap-1.5 text-xs font-medium text-foreground">
            <ShieldCheck className="h-3.5 w-3.5 text-accent-insight" />
            实际费用
          </div>
          <p className="mt-1 text-[11px] text-muted-foreground leading-normal">
            不同模型的计价和折扣不同，请以服务商账单为准。
          </p>
        </div>
      </div>
    </div>
  );
}
