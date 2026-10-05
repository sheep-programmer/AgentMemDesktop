import React, { useState, useRef, useEffect } from 'react';
import { Button } from '@/components/ui/button';
import {
  ArrowUp,
  Square,
  Sparkles,
  SlidersHorizontal,
  Cpu,
  Leaf,
  Layers,
  Combine,
  Waypoints,
  BookX,
  HardDrive,
  Cloud,
  Settings2,
} from 'lucide-react';
import { useNavigate } from 'react-router';
import { toast } from 'sonner';
import { useSpaceStore } from '@/stores/useSpaceStore';
import { providerService } from '@/lib/api/services/providers';
import { OptionCardMenu, type OptionCard } from '@/components/shared/OptionCardMenu';
import type { ProviderItem, RoleBindings } from '@/lib/api/types.temp';

type RetrievalMode = 'hybrid' | 'vector' | 'off';

const RETRIEVAL_OPTIONS: OptionCard<RetrievalMode>[] = [
  {
    value: 'hybrid',
    title: '混合检索',
    description: '语义向量 + 关键词全文，再经重排。最准，适合绝大多数问题。',
    icon: Combine,
    badge: (
      <span className="rounded-full bg-primary/10 px-1.5 py-px text-[10px] font-medium text-primary">
        推荐
      </span>
    ),
  },
  {
    value: 'vector',
    title: '仅语义向量',
    description: '只按意思相近找资料，跳过关键词与重排，更快但可能漏掉专有名词。',
    icon: Waypoints,
  },
  {
    value: 'off',
    title: '不检索',
    description: '只用模型自身知识作答，回答不会带资料出处。',
    icon: BookX,
  },
];

const CONTEXT_OPTIONS: OptionCard<'standard' | 'economy'>[] = [
  {
    value: 'standard',
    title: '标准上下文',
    description: '保留更多历史与辅助资料，适合复杂分析。',
    icon: Layers,
  },
  {
    value: 'economy',
    title: '节省上下文',
    description: '减少旧历史，优先保留相关原文，适合聚焦问答。',
    icon: Leaf,
  },
];

/** 本机推理服务的地址特征，用来给模型卡片标「本地 / 云端」。 */
function isLocalProvider(provider: ProviderItem): boolean {
  if (/ollama|lmstudio/i.test(provider.adapter || '')) return true;
  return /localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]/.test(provider.base_url || '');
}

function hostOf(url?: string | null): string {
  if (!url) return '';
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

export interface ChatSendOptions {
  useRetrieval: boolean;
  searchMode: 'hybrid' | 'vector';
  useInsights: boolean;
  contextMode: 'standard' | 'economy';
}

interface ChatInputAreaProps {
  isBusy?: boolean;
  onSend: (content: string, options: ChatSendOptions) => void;
  onAbort: () => void;
  isStreaming: boolean;
  contextMode?: 'standard' | 'economy';
  onContextModeChange?: (mode: 'standard' | 'economy') => void;
}

export function ChatInputArea({
  onSend,
  onAbort,
  isStreaming,
  isBusy = false,
  contextMode = 'standard',
  onContextModeChange,
}: ChatInputAreaProps) {
  const navigate = useNavigate();
  // 提示语跟着当前 Space 走：写死某个领域的示例，换个知识库就变成误导
  const domain = useSpaceStore((state) => state.getCurrentSpace()?.domain);
  const [text, setText] = useState('');
  const [retrievalMode, setRetrievalMode] = useState<RetrievalMode>('hybrid');
  const [insightsEnabled, setInsightsEnabled] = useState(true);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // 对话角色实际绑定的模型。选模型就是改「设置 → 模型」里 chat 角色的绑定：
  // 后端 ChatRequest 只认角色、不收模型名，在这里假装可选而不落到绑定上，等于谎报。
  const [roles, setRoles] = useState<RoleBindings | null>(null);
  const [llmProviders, setLlmProviders] = useState<ProviderItem[]>([]);
  const [switchingModel, setSwitchingModel] = useState(false);
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const [bindings, providers] = await Promise.all([
          providerService.getRoleBindings(),
          providerService.getProviders(),
        ]);
        if (!alive) return;
        setRoles(bindings);
        setLlmProviders(providers.filter((p) => p.kind === 'llm' && p.enabled !== false));
      } catch {
        // 取不到就显示「未绑定模型」，不值得为它弹错误
      }
    })();
    return () => {
      alive = false;
    };
  }, []);
  const boundProvider = llmProviders.find((p) => p.id === roles?.chat);
  const chatModel = boundProvider?.model ?? roles?.chat ?? null;

  const switchChatModel = async (providerId: string) => {
    if (!roles || providerId === roles.chat) return;
    setSwitchingModel(true);
    try {
      const response = await providerService.updateRoleBindings({ ...roles, chat: providerId });
      setRoles(response.roles);
      const picked = llmProviders.find((p) => p.id === providerId);
      toast.success(`对话模型已切换为 ${picked?.model ?? providerId}`);
    } catch (err: unknown) {
      toast.error((err as Error)?.message || '切换模型未成功，请到设置里检查');
    } finally {
      setSwitchingModel(false);
    }
  };

  const modelOptions: OptionCard<string>[] = llmProviders.map((provider) => {
    const local = isLocalProvider(provider);
    return {
      value: provider.id,
      title: provider.model || provider.id,
      description: [provider.id, hostOf(provider.base_url)].filter(Boolean).join(' · '),
      icon: local ? HardDrive : Cloud,
      badge: (
        <span
          className={
            local
              ? 'rounded-full bg-accent-insight/12 px-1.5 py-px text-[10px] font-medium text-accent-insight'
              : 'rounded-full bg-muted px-1.5 py-px text-[10px] font-medium text-muted-foreground'
          }
        >
          {local ? '本地' : '云端'}
        </span>
      ),
    };
  });

  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
      textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, 180)}px`;
    }
  }, [text]);

  const sendHint =
    'Enter 发送，Shift+Enter 换行';

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // 输入法选词时的回车是「上屏」，不是发送（中文用户最容易被误发）
    if (e.nativeEvent.isComposing || e.keyCode === 229) return;
    // 回车发送、Shift+回车换行，与常见对话应用一致；⌘/Ctrl+回车也照样发送
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  };

  const handleSubmit = () => {
    if (!text.trim() || isStreaming || isBusy) return;
    onSend(text, {
      useRetrieval: retrievalMode !== 'off',
      searchMode: retrievalMode === 'vector' ? 'vector' : 'hybrid',
      useInsights: insightsEnabled,
      contextMode,
    });
    setText('');
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }
  };

  return (
    <div className="shrink-0 px-3 pb-3 pt-2 sm:px-6 sm:pb-5">
      <div className="mx-auto max-w-[52rem] rounded-[22px] border border-border bg-card p-2.5 shadow-float transition-[border-color,box-shadow] focus-within:border-primary/45">
        <textarea
          aria-label="向知识库提问"
          ref={textareaRef}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={
            domain ? `向「${domain}」领域的专家提问…` : '向当前知识库提问…'
          }
          rows={2}
          className="w-full resize-none bg-transparent px-2.5 py-1.5 text-[15px] leading-relaxed text-foreground placeholder:text-muted-foreground outline-none"
        />

        {/* 底部选项：每个都是卡片菜单 */}
        <div className="mt-1 flex items-center justify-between gap-2">
          <div className="flex min-w-0 flex-1 items-center gap-0.5 overflow-x-auto [scrollbar-width:none]">
            <OptionCardMenu
              ariaLabel="选择检索模式"
              icon={SlidersHorizontal}
              heading="检索方式"
              label={RETRIEVAL_OPTIONS.find((o) => o.value === retrievalMode)?.title}
              value={retrievalMode}
              options={RETRIEVAL_OPTIONS}
              onChange={setRetrievalMode}
            />

            {/* 经验注入开关 */}
            <button
              type="button"
              onClick={() => setInsightsEnabled(!insightsEnabled)}
              aria-pressed={insightsEnabled}
              title={
                insightsEnabled
                  ? '回答会参考从反馈中沉淀、并经过验证的经验。点击关闭'
                  : '不参考已沉淀的经验。点击开启'
              }
              className={`flex h-7 shrink-0 items-center gap-1.5 rounded-full px-2.5 text-[12px] transition-colors ${
                insightsEnabled
                  ? 'bg-accent-insight/10 text-accent-insight hover:bg-accent-insight/15'
                  : 'text-muted-foreground hover:bg-muted hover:text-foreground'
              }`}
            >
              <Sparkles className="h-3.5 w-3.5" />
              <span>经验{insightsEnabled ? '生效' : '关闭'}</span>
            </button>

            <OptionCardMenu
              ariaLabel="选择上下文策略"
              icon={contextMode === 'economy' ? Leaf : Layers}
              heading="上下文策略"
              label={contextMode === 'economy' ? '节省上下文' : '标准上下文'}
              value={contextMode}
              options={CONTEXT_OPTIONS}
              onChange={(mode) => onContextModeChange?.(mode)}
              disabled={isStreaming || isBusy}
            />
          </div>

          <div className="flex shrink-0 items-center gap-1.5">
            {modelOptions.length > 0 ? (
              <OptionCardMenu
                ariaLabel="选择对话模型"
                icon={Cpu}
                heading="对话模型 · 改绑 chat 角色"
                label={chatModel || '未绑定模型'}
                value={roles?.chat ?? ''}
                options={modelOptions}
                onChange={(id) => void switchChatModel(id)}
                disabled={isStreaming || switchingModel}
                align="end"
                triggerClassName="font-mono text-[11.5px]"
                footer={
                  <button
                    type="button"
                    onClick={() => navigate('/settings')}
                    className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-[12px] text-muted-foreground hover:bg-muted hover:text-foreground"
                  >
                    <Settings2 className="h-3.5 w-3.5" />
                    管理模型与角色绑定
                  </button>
                }
              />
            ) : (
              <button
                type="button"
                onClick={() => navigate('/settings')}
                title="去「设置 → 模型」绑定对话模型"
                className="flex h-7 items-center gap-1.5 rounded-full px-2.5 font-mono text-[11.5px] text-muted-foreground hover:bg-muted hover:text-foreground"
              >
                <Cpu className="h-3.5 w-3.5" />
                <span className="max-w-[120px] truncate">{chatModel || '未绑定模型'}</span>
              </button>
            )}
            {isStreaming ? (
              <Button
                size="icon"
                variant="destructive"
                className="h-8 w-8 rounded-full"
                onClick={onAbort}
                aria-label="停止生成"
                title="停止生成"
              >
                <Square className="h-3 w-3 fill-current" />
              </Button>
            ) : (
              <Button
                size="icon"
                disabled={!text.trim() || isBusy}
                className="h-8 w-8 rounded-full bg-primary text-primary-foreground disabled:bg-muted disabled:text-muted-foreground disabled:opacity-100"
                onClick={handleSubmit}
                aria-label="发送问题"
                title={sendHint}
              >
                <ArrowUp className="h-4 w-4" />
              </Button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
