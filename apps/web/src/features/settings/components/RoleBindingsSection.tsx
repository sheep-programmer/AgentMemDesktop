import React, { useState } from 'react';
import type { ModelRole, RoleBindings, ProviderItem } from '@/lib/api/types.temp';
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
} from '@/components/ui/dropdown-menu';
import { Button } from '@/components/ui/button';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import { providerService } from '@/lib/api';
import { DimensionWarningDialog } from './DimensionWarningDialog';
import { SingleModelQuickSetup } from './SingleModelQuickSetup';
import {
  boundProviderIds,
  checkProviders,
  type ProviderCheckState,
} from '../lib/checkBoundProviders';
import {
  MessageSquare,
  Zap,
  Sparkles,
  Scale,
  Binary,
  Layers,
  ChevronDown,
  AlertTriangle,
  AlertCircle,
  Activity,
  Loader2,
} from 'lucide-react';

/**
 * 一次检测的结果，连同检测时的服务商对象一起记下。
 *
 * 服务商被编辑、或在下方列表里单独测过之后，父组件会换成一个新对象；这时旧结果已经
 * 不代表当前配置（可能改了地址或密钥），按引用比较就能让它自动作废，退回列表给的状态，
 * 而不是继续挂着一个过期的「在线」。
 */
interface CheckRecord {
  provider: ProviderItem;
  state: ProviderCheckState;
}

interface RoleBindingsSectionProps {
  bindings: RoleBindings;
  providers: ProviderItem[];
  onUpdateBinding: (role: ModelRole, providerId: string) => void;
  /** 一次提交多个角色的绑定，「用一个模型搞定」用它，避免逐个提交四次 */
  onUpdateBindings: (next: RoleBindings, successMessage: string) => Promise<boolean>;
}

export function RoleBindingsSection({
  bindings,
  providers,
  onUpdateBinding,
  onUpdateBindings,
}: RoleBindingsSectionProps) {
  const [isDimWarningOpen, setIsDimWarningOpen] = useState(false);
  const [pendingDimProviderId, setPendingDimProviderId] = useState<string | null>(null);
  const [checks, setChecks] = useState<Record<string, CheckRecord>>({});
  const [isCheckingAll, setIsCheckingAll] = useState(false);
  const [isAdvancedOpen, setIsAdvancedOpen] = useState(true);

  const targetIds = boundProviderIds(bindings, providers.map((p) => p.id));

  const handleCheckAll = async () => {
    const targets = targetIds
      .map((id) => providers.find((p) => p.id === id))
      .filter((p): p is ProviderItem => p !== undefined);
    if (targets.length === 0) return;
    const snapshot = new Map(targets.map((p) => [p.id, p]));
    setIsCheckingAll(true);
    setChecks((prev) => {
      const next = { ...prev };
      for (const p of targets) next[p.id] = { provider: p, state: { status: 'checking' } };
      return next;
    });
    try {
      await checkProviders(
        targets.map((p) => p.id),
        (id) => providerService.checkHealth(id),
        (id, state) => {
          setChecks((prev) => ({ ...prev, [id]: { provider: snapshot.get(id)!, state } }));
        },
      );
    } finally {
      setIsCheckingAll(false);
    }
  };

  const roleMeta: Record<ModelRole, { label: string; desc: string; icon: React.ReactNode; defaultKind: string }> = {
    chat: { label: '主对话推理 (Chat)', desc: '负责领域问答、推理与方案论证', icon: <MessageSquare className="h-4 w-4 text-primary" />, defaultKind: 'llm' },
    fast: { label: '高速意图识别 (Fast)', desc: '负责查询改写、意图分类与快速路由', icon: <Zap className="h-4 w-4 text-accent-ai" />, defaultKind: 'llm' },
    distill: { label: '经验蒸馏反思 (Distill)', desc: '负责从纠错与反馈中总结经验', icon: <Sparkles className="h-4 w-4 text-accent-insight" />, defaultKind: 'llm' },
    judge: { label: '评测打分 (Judge)', desc: '负责评测集问答对比与客观打分', icon: <Scale className="h-4 w-4 text-accent-warn" />, defaultKind: 'llm' },
    embedding: { label: '向量嵌入表征 (Embedding)', desc: '负责把资料段落与经验转成向量，用于语义检索', icon: <Binary className="h-4 w-4 text-primary" />, defaultKind: 'embedding' },
    rerank: { label: '重排交叉编码 (Rerank)', desc: '负责语义与关键词候选切片的深度重打分', icon: <Layers className="h-4 w-4 text-accent-ai" />, defaultKind: 'rerank' },
  };

  const handleSelectProvider = (role: ModelRole, prov: ProviderItem) => {
    if (role === 'embedding') {
      // 选回当前这个不算更换，不必弹重建确认
      if (bindings.embedding === prov.id) return;
      setPendingDimProviderId(prov.id);
      setIsDimWarningOpen(true);
      return;
    }
    onUpdateBinding(role, prov.id);
  };

  const confirmDimChange = () => {
    if (pendingDimProviderId) {
      onUpdateBinding('embedding', pendingDimProviderId);
    }
    setIsDimWarningOpen(false);
  };

  return (
    <div className="rounded-2xl border border-border bg-card p-6 space-y-4">
      <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-foreground">模型角色分工</h3>
          <p className="text-xs text-muted-foreground mt-0.5">
            AgentMem 采用分工特化架构，各个业务环节分别绑定最适宜的 Provider，兼顾高智商推理与低延迟。
          </p>
        </div>

        <div className="flex flex-col items-start sm:items-end gap-1 shrink-0">
          <Button
            size="sm"
            variant="outline"
            onClick={handleCheckAll}
            disabled={isCheckingAll || targetIds.length === 0}
            className="gap-1.5 text-xs"
          >
            {isCheckingAll ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Activity className="h-3.5 w-3.5 text-primary" />
            )}
            {isCheckingAll ? '检测中…' : '检测全部连接'}
          </Button>
          <p className="text-[10px] text-muted-foreground">
            会向每个已绑定的服务发一次很小的测试请求
          </p>
        </div>
      </div>

      <SingleModelQuickSetup bindings={bindings} providers={providers} onApply={onUpdateBindings} />

      <button
        type="button"
        onClick={() => setIsAdvancedOpen((v) => !v)}
        aria-expanded={isAdvancedOpen}
        className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground hover:text-foreground"
      >
        <ChevronDown className={`h-3.5 w-3.5 transition-transform ${isAdvancedOpen ? '' : '-rotate-90'}`} />
        分别设置（高级）
      </button>

      {isAdvancedOpen && (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {(Object.keys(roleMeta) as ModelRole[]).map((role) => {
            const meta = roleMeta[role];
            const providerId = bindings[role];
            const boundProvider = providers.find((p) => p.id === providerId);
            const eligibleProviders = providers.filter((p) => p.kind === meta.defaultKind);
            const record = boundProvider ? checks[boundProvider.id] : undefined;
            // 服务商对象换过（被编辑或单独测过）就不再用这次批量检测的结果
            const check = record && record.provider === boundProvider ? record.state : undefined;
            const isOffline = boundProvider?.status === 'offline';
            const isFailed = check?.status === 'failed';

            return (
              <div
                key={role}
                className={`flex flex-col justify-between rounded-xl border p-4 text-xs shadow-2xs space-y-3 transition-colors ${
                  isOffline || isFailed
                    ? 'border-destructive/40 bg-destructive/[0.02]'
                    : 'border-border/70 bg-background/60'
                }`}
              >
                <div>
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2 font-medium text-foreground">
                      {meta.icon}
                      <span>{meta.label}</span>
                    </div>
                    {isOffline && (
                      <span className="flex items-center gap-1 text-[10px] text-destructive bg-destructive/10 px-1.5 py-0.5 rounded font-medium">
                        <AlertTriangle className="h-3 w-3" />
                        不可用
                      </span>
                    )}
                  </div>
                  <p className="mt-1 text-[11px] text-muted-foreground leading-snug">
                    {meta.desc}
                  </p>
                </div>

                <div className="border-t border-border/40 pt-2.5 flex items-center justify-between">
                  <DropdownMenu>
                    <DropdownMenuTrigger className="flex items-center gap-1.5 rounded-md border border-border bg-card px-2.5 py-1 text-xs text-foreground hover:bg-muted outline-none max-w-[200px]">
                      <span className="truncate">{boundProvider ? (boundProvider.name || boundProvider.id) : '选择服务商...'}</span>
                      <ChevronDown className="h-3 w-3 opacity-60 shrink-0" />
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="start">
                      {eligibleProviders.length === 0 ? (
                        <div className="p-2 text-[11px] text-muted-foreground">暂无此类型的可用服务商</div>
                      ) : (
                        eligibleProviders.map((prov) => (
                          <DropdownMenuItem
                            key={prov.id}
                            onClick={() => handleSelectProvider(role, prov)}
                            className="text-xs"
                          >
                            {prov.name || prov.id} · {prov.model}
                          </DropdownMenuItem>
                        ))
                      )}
                    </DropdownMenuContent>
                  </DropdownMenu>

                  <div className="flex items-center gap-1 font-mono text-[10px] min-w-0" title={check ? undefined : '服务商连通状态'}>
                    {!boundProvider ? (
                      <span className="flex items-center gap-1 text-accent-warn">
                        <AlertCircle className="h-3 w-3" />
                        未绑定
                      </span>
                    ) : check?.status === 'checking' ? (
                      <span className="flex items-center gap-1 text-muted-foreground">
                        <Loader2 className="h-3 w-3 animate-spin" />
                        检测中
                      </span>
                    ) : check?.status === 'online' ? (
                      <span className="flex items-center gap-1 text-accent-insight">
                        <span className="h-1.5 w-1.5 rounded-full bg-accent-insight" />
                        在线{check.latencyMs !== null ? ` · ${check.latencyMs}ms` : ''}
                      </span>
                    ) : check?.status === 'failed' ? (
                      <Tooltip>
                        <TooltipTrigger className="flex items-center gap-1 text-destructive font-semibold min-w-0 max-w-[120px] cursor-help border-0 bg-transparent p-0">
                          <span className="h-1.5 w-1.5 rounded-full bg-destructive shrink-0" />
                          <span className="truncate">失败 · {check.error}</span>
                        </TooltipTrigger>
                        <TooltipContent side="top" className="text-xs max-w-sm break-all whitespace-pre-wrap">
                          {check.error}
                        </TooltipContent>
                      </Tooltip>
                    ) : isOffline ? (
                      <span className="flex items-center gap-1 text-destructive font-semibold">
                        <span className="h-1.5 w-1.5 rounded-full bg-destructive" />
                        离线
                      </span>
                    ) : boundProvider.latency_ms ? (
                      <span className="flex items-center gap-1 text-accent-insight">
                        <span className="h-1.5 w-1.5 rounded-full bg-accent-insight animate-pulse" />
                        {boundProvider.latency_ms}ms
                      </span>
                    ) : (
                      <span className="flex items-center gap-1 text-muted-foreground">
                        <span className="h-1.5 w-1.5 rounded-full bg-muted-foreground/40" />
                        待测
                      </span>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}

      <DimensionWarningDialog
        isOpen={isDimWarningOpen}
        onClose={() => setIsDimWarningOpen(false)}
        onConfirm={confirmDimChange}
        oldDim={providers.find((p) => p.id === bindings.embedding)?.dimension ?? undefined}
        newDim={providers.find((p) => p.id === pendingDimProviderId)?.dimension ?? undefined}
      />
    </div>
  );
}
