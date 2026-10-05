import { useState } from 'react';
import type { ProviderItem, RoleBindings } from '@/lib/api/types.temp';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import {
  applyChanges,
  planSingleModel,
  type BindingChange,
  ROLE_NAMES,
  sharedLlmProvider,
} from '../lib/singleModelPlan';
import { ArrowRight, ChevronDown, Loader2, Wand2 } from 'lucide-react';

interface SingleModelQuickSetupProps {
  bindings: RoleBindings;
  providers: ProviderItem[];
  /** 一次提交完整绑定；成功返回 true，失败时由调用方回滚并提示 */
  onApply: (next: RoleBindings, successMessage: string) => Promise<boolean>;
}

/** 显示用的服务商名字；绑定指向已删除的服务商时照实说 */
function providerLabel(providers: ProviderItem[], id: string | null | undefined): string {
  if (!id) return '未绑定';
  const prov = providers.find((p) => p.id === id);
  return prov ? prov.name || prov.id : `${id}（已不存在）`;
}

/**
 * 「只有一个大模型」时的快捷设置：一次把对话、改写、经验总结、评测打分交给同一个服务商。
 *
 * 普通用户往往只有一个 API Key，面对六个下拉框不知从何下手。这里只动四个 LLM 角色，
 * 向量和重排保持原样——它们需要专门的模型，对话模型给不了。
 */
export function SingleModelQuickSetup({ bindings, providers, onApply }: SingleModelQuickSetupProps) {
  const llmProviders = providers.filter((p) => p.kind === 'llm');
  const shared = sharedLlmProvider(bindings);
  // 四个角色还没统一时多半是刚上手，直接展开；已经统一的老用户不必每次看到
  const [isOpen, setIsOpen] = useState(shared === null);
  const [pickedId, setPickedId] = useState<string | null>(null);
  // 点「应用」那一刻的改动清单；确认框只看它，不随乐观更新后的绑定变化而清空
  const [pending, setPending] = useState<{ label: string; changes: BindingChange[] } | null>(null);
  const [isApplying, setIsApplying] = useState(false);

  // 没手动选过就跟着当前绑定走：已统一选它，否则选主对话在用的，再不然选第一个
  const defaultId =
    llmProviders.find((p) => p.id === shared)?.id ??
    llmProviders.find((p) => p.id === bindings.chat)?.id ??
    llmProviders[0]?.id ??
    null;
  const selectedId = llmProviders.some((p) => p.id === pickedId) ? pickedId : defaultId;
  const changes = selectedId ? planSingleModel(bindings, selectedId) : [];
  const alreadyApplied = selectedId !== null && changes.length === 0;
  const selectedLabel = providerLabel(providers, selectedId);

  const handleConfirm = async () => {
    if (!pending) return;
    setIsApplying(true);
    try {
      const ok = await onApply(
        applyChanges(bindings, pending.changes),
        `已把对话、改写、经验总结、评测打分交给「${pending.label}」`,
      );
      if (ok) setPending(null);
    } finally {
      setIsApplying(false);
    }
  };

  return (
    <div className="rounded-xl border border-primary/25 bg-primary/[0.03] text-xs">
      <button
        type="button"
        onClick={() => setIsOpen((v) => !v)}
        aria-expanded={isOpen}
        className="flex w-full items-center justify-between gap-2 px-4 py-3 text-left"
      >
        <span className="flex items-center gap-2 font-medium text-foreground">
          <Wand2 className="h-4 w-4 text-primary shrink-0" />
          只有一个大模型？一键把对话、改写、经验总结、评测打分都交给它
        </span>
        <ChevronDown className={`h-4 w-4 opacity-60 shrink-0 transition-transform ${isOpen ? 'rotate-180' : ''}`} />
      </button>

      {isOpen && (
        <div className="border-t border-primary/15 px-4 py-3 space-y-2.5">
          {llmProviders.length === 0 ? (
            <p className="text-muted-foreground">
              还没有语言模型类服务商，先在下方「模型服务商」里添加一个。
            </p>
          ) : (
            <div className="flex flex-col sm:flex-row sm:items-center gap-2">
              <select
                value={selectedId ?? ''}
                onChange={(e) => setPickedId(e.target.value)}
                disabled={isApplying}
                aria-label="选择要统一使用的大模型"
                className="w-full sm:w-72 rounded-md border border-input bg-background px-2.5 py-1.5 text-xs outline-none"
              >
                {llmProviders.map((prov) => (
                  <option key={prov.id} value={prov.id}>
                    {prov.name || prov.id} · {prov.model}
                  </option>
                ))}
              </select>
              <Button
                size="sm"
                onClick={() => setPending({ label: selectedLabel, changes })}
                disabled={!selectedId || alreadyApplied || isApplying}
                className="text-xs shrink-0"
              >
                应用
              </Button>
              {alreadyApplied && (
                <span className="text-[11px] text-muted-foreground">已经是这个模型</span>
              )}
            </div>
          )}

          <p className="text-[11px] text-muted-foreground leading-snug">
            向量嵌入（当前：{providerLabel(providers, bindings.embedding)}）和重排（当前：
            {providerLabel(providers, bindings.rerank)}）不会改动：它们需要专门的向量模型和重排模型，普通对话模型提供不了。
          </p>
        </div>
      )}

      <Dialog open={pending !== null} onOpenChange={(open) => !open && !isApplying && setPending(null)}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle className="text-base font-semibold">统一交给「{pending?.label}」？</DialogTitle>
            <DialogDescription className="text-xs text-muted-foreground pt-2 leading-relaxed">
              以下角色会改绑，确认后立即生效；向量嵌入和重排保持不变。
            </DialogDescription>
          </DialogHeader>

          <ul className="rounded-lg border border-border bg-muted/30 p-3 text-xs space-y-1.5">
            {pending?.changes.map((change) => (
              <li key={change.role} className="flex items-center gap-2">
                <span className="w-16 shrink-0 font-medium text-foreground">{ROLE_NAMES[change.role]}</span>
                <span className="truncate text-muted-foreground">{providerLabel(providers, change.from)}</span>
                <ArrowRight className="h-3 w-3 shrink-0 text-muted-foreground" />
                <span className="truncate text-foreground">{pending?.label}</span>
              </li>
            ))}
          </ul>

          <div className="mt-4 flex items-center justify-end gap-2">
            <Button variant="outline" size="sm" onClick={() => setPending(null)} disabled={isApplying}>
              取消
            </Button>
            <Button size="sm" onClick={handleConfirm} disabled={isApplying} className="gap-1.5">
              {isApplying && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              {isApplying ? '应用中…' : '确认改绑'}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
