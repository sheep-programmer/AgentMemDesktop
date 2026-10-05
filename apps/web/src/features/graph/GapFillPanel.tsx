import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router';
import { toast } from 'sonner';
import { FilePlus2, Loader2, PenLine, Plus } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { cn } from '@/lib/utils';
import { formatShortcut } from '@/lib/platform';
import { memoryService } from '@/lib/api/services/memory';
import type { KnowledgeMapTopic } from '@/lib/api/services/knowledgeMap';
import { CARD_KIND_LABEL, CARD_SATURATION, IMPORTANCE_LABEL, isSolid } from './graphModel';

type CardKind = 'concept' | 'fact' | 'procedure' | 'pitfall' | 'tool';
const CARD_KINDS: CardKind[] = ['concept', 'fact', 'procedure', 'pitfall', 'tool'];

/** 已有几张、还差几张：和图上主题旁边的虚线小球一一对应 */
export function SlotDots({ filled, className }: { filled: number; className?: string }) {
  return (
    <span className={cn('inline-flex items-center gap-[3px]', className)} aria-hidden>
      {Array.from({ length: CARD_SATURATION }, (_, index) => (
        <span
          key={index}
          className={cn(
            'h-[7px] w-[7px] rounded-full transition-colors duration-500',
            index < filled ? 'bg-accent-insight' : 'border border-dashed border-muted-foreground/60',
          )}
        />
      ))}
    </span>
  );
}

interface GapFillPanelProps {
  spaceId: string;
  topic: KnowledgeMapTopic;
  /** 当前（时间轴所在时刻）命中的卡片数 */
  count: number;
  mastery: number;
  /** 从某个待补槽位点进来时，槽位上建议的子主题 */
  suggestion?: string | null;
  /** 缺口 / 偏薄的主题默认展开表单 */
  defaultOpen: boolean;
  onFilled: (cardTitle: string) => void;
}

/**
 * 补上一个缺口：手写一张卡片，或者去导入相关资料。
 *
 * 手写的卡片把主题名（和选中的子主题）写进别名——覆盖判定看的是标题与别名，
 * 这样新卡片一定落在这个主题上，图上的虚线球当场被它补上。
 */
export function GapFillPanel({ spaceId, topic, count, mastery, suggestion, defaultOpen, onFilled }: GapFillPanelProps) {
  const navigate = useNavigate();
  const [open, setOpen] = useState(defaultOpen);
  const [title, setTitle] = useState(suggestion ?? '');
  const [subtopic, setSubtopic] = useState<string | null>(suggestion ?? null);
  const [kind, setKind] = useState<CardKind>('concept');
  const [body, setBody] = useState('');
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  const titleRef = useRef<HTMLInputElement>(null);

  // 换了一个主题 / 槽位：表单跟着换
  useEffect(() => {
    setOpen(defaultOpen);
    setTitle(suggestion ?? '');
    setSubtopic(suggestion && (topic.subtopics ?? []).includes(suggestion) ? suggestion : null);
    setBody('');
  }, [topic.topic, suggestion]);

  useEffect(() => {
    if (open && suggestion) titleRef.current?.focus();
  }, [open, suggestion]);

  const subtopics = topic.subtopics ?? [];
  const missing = Math.max(0, CARD_SATURATION - count);
  const canSave = title.trim().length > 0 && body.trim().length > 0 && !saving;

  const save = async () => {
    if (!canSave || savingRef.current) return;
    savingRef.current = true;
    setSaving(true);
    try {
      const aliases = [topic.topic, subtopic].filter(
        (alias): alias is string => !!alias && !title.includes(alias),
      );
      await memoryService.createCard(spaceId, {
        kind,
        title: title.trim(),
        body: body.trim(),
        aliases,
        // 人写的卡片按已核验处理，与后端缺省一致
        confidence: 1,
        verified_by: 'user',
      });
      toast.success(`已补上「${title.trim()}」`, { description: `归入主题：${topic.topic}` });
      onFilled(title.trim());
      setTitle('');
      setBody('');
      setSubtopic(null);
    } catch (err) {
      toast.error((err as Error)?.message || '保存卡片失败');
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  };

  return (
    <div className="mt-3 rounded-xl border border-dashed border-border bg-muted/30 p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-[12px] font-medium text-foreground">
            <SlotDots filled={Math.min(count, CARD_SATURATION)} />
            {missing > 0 ? (
              <span>
                {count === 0 ? '尚未积累知识' : `已有 ${count} 张卡片`} · 可再补 {missing} 张
              </span>
            ) : (
              <span>已有 {count} 张卡片 · {isSolid(count, mastery) ? '覆盖较扎实' : '仍可补充和核验'}</span>
            )}
          </div>
          <div className="mt-0.5 text-[11px] text-muted-foreground">
            {IMPORTANCE_LABEL[topic.importance] ?? topic.importance}主题 · 数量与置信度共同影响掌握度
          </div>
        </div>
        {!open && (
          <Button size="sm" variant="outline" className="h-7 shrink-0 gap-1 text-[11.5px]" onClick={() => setOpen(true)}>
            <Plus className="h-3 w-3" />
            补一张
          </Button>
        )}
      </div>

      {subtopics.length > 0 && (
        <div className="mt-2.5">
          <div className="mb-1 text-[11px] text-muted-foreground">可以从这些子主题补起</div>
          <div className="flex flex-wrap gap-1">
            {subtopics.map((item) => (
              <button
                key={item}
                disabled={saving}
                type="button"
                onClick={() => {
                  setOpen(true);
                  setSubtopic(item);
                  if (!title.trim() || subtopics.includes(title.trim())) setTitle(item);
                  requestAnimationFrame(() => titleRef.current?.focus());
                }}
                className={cn(
                  'rounded-full border px-2 py-0.5 text-[11px] transition-colors',
                  subtopic === item
                    ? 'border-primary/50 bg-primary/10 text-primary'
                    : 'border-dashed border-border text-muted-foreground hover:border-primary/40 hover:text-foreground',
                )}
              >
                {item}
              </button>
            ))}
          </div>
        </div>
      )}

      {open && (
        <form
          className="mt-3 animate-in fade-in-0 slide-in-from-top-1 duration-200"
          aria-busy={saving}
          onSubmit={(event) => {
            event.preventDefault();
            void save();
          }}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
              event.preventDefault();
              void save();
            }
          }}
        >
          <fieldset disabled={saving} className="space-y-2 disabled:opacity-70">
            <div className="flex flex-wrap gap-1" role="radiogroup" aria-label="卡片类型">
              {CARD_KINDS.map((item) => (
                <button
                  key={item}
                  type="button"
                  role="radio"
                  aria-checked={kind === item}
                  onClick={() => setKind(item)}
                  className={cn(
                    'rounded-md px-2 py-0.5 text-[11px] transition-colors',
                    kind === item ? 'bg-foreground text-background' : 'text-muted-foreground hover:bg-muted',
                  )}
                >
                  {CARD_KIND_LABEL[item]}
                </button>
              ))}
            </div>
            <Input
              ref={titleRef}
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              placeholder={`标题，如「${subtopics[0] ?? topic.topic}」`}
              className="h-8 bg-card text-[12.5px]"
              aria-label="卡片标题"
            />
            <Textarea
              value={body}
              onChange={(event) => setBody(event.target.value)}
              placeholder="写下这条知识：定义、要点、数值或步骤都可以"
              className="min-h-[76px] bg-card text-[12.5px] leading-relaxed"
              aria-label="卡片正文"
            />
            <div className="flex items-center justify-between gap-2">
              <span className="text-[10.5px] text-muted-foreground">{formatShortcut('⌘ Enter')} 保存 · 按已核验计入</span>
              <div className="flex gap-1.5">
                <Button type="button" size="sm" variant="ghost" className="h-7 text-[11.5px]" onClick={() => setOpen(false)}>
                  收起
                </Button>
                <Button type="submit" size="sm" className="h-7 gap-1 text-[11.5px]" disabled={!canSave}>
                  {saving ? <Loader2 className="h-3 w-3 animate-spin" /> : <PenLine className="h-3 w-3" />}
                  补上
                </Button>
              </div>
            </div>
          </fieldset>
        </form>
      )}

      <button
        type="button"
        onClick={() => navigate(`/s/${spaceId}/library?import=1`)}
        className="mt-2.5 flex w-full items-center gap-2 rounded-lg px-1 py-1 text-left text-[11.5px] text-muted-foreground transition-colors hover:text-foreground"
      >
        <FilePlus2 className="h-3.5 w-3.5 text-accent-ai" />
        或者导入讲「{topic.topic}」的资料，让它自己抽取卡片
      </button>
    </div>
  );
}
