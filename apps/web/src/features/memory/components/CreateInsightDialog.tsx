import React, { useState } from 'react';
import type { Insight, InsightRequest } from '@/lib/api/types';
import { memoryService } from '@/lib/api/services/memory';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { Loader2, Lightbulb } from 'lucide-react';
import { toast } from 'sonner';

type InsightKind = InsightRequest['kind'];

/** 经验类型：给出中文说法和一句提示，免得用户对着五个英文词猜。 */
const KIND_OPTIONS: Array<{ value: InsightKind; label: string; hint: string }> = [
  { value: 'heuristic', label: '经验法则', hint: '遇到某类问题时的思路或步骤' },
  { value: 'correction', label: '纠错', hint: '模型常犯的错误与正确说法' },
  { value: 'constraint', label: '约束', hint: '回答时必须遵守的规则或边界' },
  { value: 'preference', label: '偏好', hint: '回答的格式、口吻、详略' },
  { value: 'terminology', label: '术语', hint: '领域词汇的准确含义或统一译法' },
];

interface CreateInsightDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  spaceId: string;
  onCreated: (insight: Insight) => void;
}

const fieldClass = 'text-xs md:text-xs';

/**
 * 手动新增一条经验。
 *
 * 后端的人工新增接口早就有（直接设为生效并向量化），前端却一直没有入口：经验只能
 * 靠对话里的反馈慢慢蒸馏出来。领域专家心里现成的规则，没有理由非得绕一圈反馈。
 */
export function CreateInsightDialog({ open, onOpenChange, spaceId, onCreated }: CreateInsightDialogProps) {
  const [trigger, setTrigger] = useState('');
  const [guidance, setGuidance] = useState('');
  const [rationale, setRationale] = useState('');
  const [kind, setKind] = useState<InsightKind>('heuristic');
  const [submitting, setSubmitting] = useState(false);

  const canSubmit = trigger.trim().length > 0 && guidance.trim().length > 0 && !submitting;

  const reset = () => {
    setTrigger('');
    setGuidance('');
    setRationale('');
    setKind('heuristic');
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    try {
      const created = await memoryService.createInsight(spaceId, {
        trigger: trigger.trim(),
        guidance: guidance.trim(),
        rationale: rationale.trim() || null,
        kind,
        scope: 'space',
        confidence: 0.8,
      });
      onCreated(created);
      toast.success('已新增经验，之后的相关提问会参考它');
      reset();
      onOpenChange(false);
    } catch (err: unknown) {
      // 失败时保留已填内容：一段写好的做法说明丢了要重写
      toast.error((err as Error)?.message || '新增经验失败');
    } finally {
      setSubmitting(false);
    }
  };

  const activeHint = KIND_OPTIONS.find((o) => o.value === kind)?.hint;

  return (
    <Dialog open={open} onOpenChange={(next) => !submitting && onOpenChange(next)}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <div className="flex items-center gap-2">
            <Lightbulb className="h-5 w-5 text-accent-insight" />
            <DialogTitle className="text-base font-semibold">新增经验</DialogTitle>
          </div>
          <DialogDescription className="pt-1 text-xs leading-relaxed text-muted-foreground">
            手写的经验视为已确认，保存后立即生效：提问命中触发条件时，会按这里的做法作答。
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="space-y-3 text-xs">
          <div className="space-y-1">
            <label htmlFor="insight-trigger" className="font-medium text-foreground">
              触发条件 <span className="text-destructive">*</span>
            </label>
            <Textarea
              id="insight-trigger"
              value={trigger}
              onChange={(e) => setTrigger(e.target.value)}
              placeholder="什么情况下用这条经验，如「用户询问先导化合物的心脏毒性风险时」"
              className={`${fieldClass} min-h-12`}
              maxLength={500}
              autoFocus
            />
          </div>

          <div className="space-y-1">
            <label htmlFor="insight-guidance" className="font-medium text-foreground">
              做法 <span className="text-destructive">*</span>
            </label>
            <Textarea
              id="insight-guidance"
              value={guidance}
              onChange={(e) => setGuidance(e.target.value)}
              placeholder="应该怎么答，如「先给出 hERG IC50 的判定阈值，再说明需补做的实验」"
              className={`${fieldClass} min-h-20`}
              maxLength={2000}
            />
          </div>

          <div className="space-y-1">
            <label htmlFor="insight-rationale" className="font-medium text-foreground">
              理由 <span className="font-normal text-muted-foreground">（可选）</span>
            </label>
            <Textarea
              id="insight-rationale"
              value={rationale}
              onChange={(e) => setRationale(e.target.value)}
              placeholder="为什么这样做，方便日后复查这条经验是否还成立"
              className={`${fieldClass} min-h-12`}
              maxLength={1000}
            />
          </div>

          <div className="space-y-1">
            <label htmlFor="insight-kind" className="font-medium text-foreground">
              类型
            </label>
            <select
              id="insight-kind"
              value={kind}
              onChange={(e) => setKind(e.target.value as InsightKind)}
              className="w-full rounded-md border border-input bg-background px-2.5 py-1.5 text-xs outline-none"
            >
              {KIND_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
            {activeHint && <p className="text-[11px] text-muted-foreground">{activeHint}</p>}
          </div>

          <div className="flex items-center justify-end gap-2 pt-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => onOpenChange(false)}
              disabled={submitting}
            >
              取消
            </Button>
            <Button type="submit" size="sm" disabled={!canSubmit} className="gap-1.5">
              {submitting && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              保存并启用
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
