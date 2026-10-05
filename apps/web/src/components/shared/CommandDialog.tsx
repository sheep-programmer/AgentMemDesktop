import React, { useState, useEffect } from 'react';
import { formatShortcut } from '@/lib/platform';
import { useNavigate } from 'react-router';
import { useUiStore } from '@/stores/useUiStore';
import { useSpaceStore } from '@/stores/useSpaceStore';
import { useThemeStore } from '@/stores/useThemeStore';
import {
  CommandDialog as CmdkDialog,
  CommandInput,
  CommandList,
  CommandEmpty,
  CommandGroup,
  CommandItem,
  CommandSeparator,
} from '@/components/ui/command';
import {
  MessageSquarePlus,
  Upload,
  Sparkles,
  Sun,
  Moon,
  Laptop,
  Layers,
  Cpu,
  ArrowRight,
  BookOpen,
  Brain,
  BarChart3,
  PanelRight,
  FileText,
} from 'lucide-react';
import type { components } from '@/lib/api/types.gen';
type SearchResponse = components['schemas']['SearchResponse'];
import { toast } from 'sonner';
import { apiClient, isMockMode } from '@/lib/api/client';
import { mockDocuments, mockKnowledgeCards } from '@/lib/api/mock/data';

interface SearchHitItem {
  id: string;
  title: string;
  snippet?: string;
  score?: number;
  type: 'document' | 'card';
  documentId?: string;
  chunkId?: string;
}

export function CommandDialog() {
  const navigate = useNavigate();
  const {
    isCommandOpen,
    setCommandOpen,
    setSpaceSwitcherOpen,
    toggleEvidence,
    openReader,
  } = useUiStore();
  const { spaces, currentSpaceId, setCurrentSpaceId, getCurrentSpace } =
    useSpaceStore();
  const currentSpaceDomain = getCurrentSpace()?.domain;
  const { theme, setTheme, resolvedTheme } = useThemeStore();
  const [searchQuery, setSearchQuery] = useState('');
  const [searchHits, setSearchHits] = useState<SearchHitItem[]>([]);
  const [isSearching, setIsSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);

  useEffect(() => {
    if (!isCommandOpen) return;
    setSearchQuery('');
    setSearchHits([]);
  }, [isCommandOpen]);

  const handleSelect = (callback: () => void) => {
    callback();
    setCommandOpen(false);
  };

  useEffect(() => {
    const q = searchQuery.trim();
    setSearchHits([]);
    setSearchError(null);
    setIsSearching(false);
    if (!isCommandOpen || q.length < 2 || !currentSpaceId) return;
    const controller = new AbortController();
    setIsSearching(true);
    const timer = window.setTimeout(async () => {
      try {
        if (isMockMode()) {
          const lower = q.toLowerCase();
          const hits: SearchHitItem[] = [
            ...mockDocuments
              .filter(
                (document) =>
                  document.space_id === currentSpaceId &&
                  document.title.toLowerCase().includes(lower),
              )
              .slice(0, 3)
              .map((document) => ({
                id: document.id,
                documentId: document.id,
                title: document.title,
                snippet: `${document.token_count} 词元`,
                type: 'document' as const,
              })),
            ...mockKnowledgeCards
              .filter(
                (card) =>
                  card.space_id === currentSpaceId &&
                  `${card.title} ${card.body}`.toLowerCase().includes(lower),
              )
              .slice(0, 3)
              .map((card) => ({
                id: card.id,
                title: card.title,
                snippet: card.body.slice(0, 50),
                type: 'card' as const,
              })),
          ];
          if (!controller.signal.aborted) setSearchHits(hits);
        } else {
          const result = await apiClient<SearchResponse>(
            `/spaces/${currentSpaceId}/search`,
            {
              method: 'POST',
              body: JSON.stringify({ query: q, top_k: 5 }),
              signal: controller.signal,
              timeoutMs: 30_000,
            },
          );
          if (!controller.signal.aborted)
            setSearchHits(
              (result.hits || []).map((hit) => ({
                id: hit.chunk_id,
                documentId: hit.document_id,
                chunkId: hit.chunk_id,
                title: hit.document_title || '未命名资料',
                snippet: hit.snippet,
                score: hit.score,
                type: 'document' as const,
              })),
            );
        }
      } catch (error) {
        if (!controller.signal.aborted)
          setSearchError(
            error instanceof Error
              ? error.message
              : '搜索暂时不可用，请稍后重试。',
          );
      } finally {
        if (!controller.signal.aborted) setIsSearching(false);
      }
    }, 250);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [searchQuery, currentSpaceId, isCommandOpen]);

  return (
    <CmdkDialog open={isCommandOpen} onOpenChange={setCommandOpen}>
      <CommandInput
        aria-label="搜索知识或命令"
        maxLength={200}
        placeholder={
          currentSpaceDomain
            ? `输入命令或搜索「${currentSpaceDomain}」知识... (如: 进化, 切换主题)`
            : '输入命令或搜索私域知识... (如: 进化, 切换主题)'
        }
        value={searchQuery}
        onValueChange={setSearchQuery}
      />
      <CommandList className="max-h-[min(380px,60dvh)] overflow-y-auto p-2">
        {searchHits.length === 0 && (
          <CommandEmpty>
            {isSearching
              ? '正在检索知识库…'
              : searchError || '未找到匹配的指令或知识条目'}
          </CommandEmpty>
        )}

        {/* 实时知识库搜索结果 */}
        {searchHits.length > 0 && (
          <>
            <CommandGroup
              forceMount
              heading={`知识库检索命中 (${searchHits.length})`}
            >
              {searchHits.map((hit) => (
                <CommandItem
                  key={hit.id}
                  forceMount
                  value={`知识结果:${hit.id}`}
                  onSelect={() =>
                    handleSelect(() => {
                      if (hit.type === 'document') {
                        openReader({
                          documentId: hit.documentId || hit.id,
                          documentTitle: hit.title,
                          chunkId: hit.chunkId,
                          spaceId: currentSpaceId,
                        });
                      } else {
                        navigate(
                          `/s/${currentSpaceId}/memory?q=${encodeURIComponent(hit.title)}`,
                        );
                      }
                    })
                  }
                >
                  <FileText className="mr-2 h-4 w-4 text-primary shrink-0" />
                  <div className="flex flex-1 flex-col min-w-0">
                    <span className="font-medium text-foreground truncate">
                      {hit.title}
                    </span>
                    {hit.snippet && (
                      <span className="text-[11px] text-muted-foreground truncate">
                        {hit.snippet}
                      </span>
                    )}
                  </div>
                  <span className="ml-auto font-mono text-[10px] text-muted-foreground">
                    {hit.type === 'document' ? '文献' : '卡片'}
                  </span>
                </CommandItem>
              ))}
            </CommandGroup>
            <CommandSeparator className="my-1" />
          </>
        )}

        {/* 快捷行动 */}
        <CommandGroup heading="快捷行动">
          <CommandItem
            onSelect={() =>
              handleSelect(() => {
                if (currentSpaceId) navigate(`/s/${currentSpaceId}/chat?new=1`);
                else setSpaceSwitcherOpen(true);
              })
            }
          >
            <MessageSquarePlus className="mr-2 h-4 w-4 text-primary" />
            <span>新建对话</span>
            <span className="ml-auto font-mono text-xs text-muted-foreground">
              {formatShortcut('⌘N')}
            </span>
          </CommandItem>
          <CommandItem
            onSelect={() =>
              handleSelect(() => {
                navigate(`/s/${currentSpaceId}/library?import=1`);
              })
            }
          >
            <Upload className="mr-2 h-4 w-4 text-accent-ai" />
            <span>导入资料到当前知识空间</span>
            <span className="ml-auto font-mono text-xs text-muted-foreground">
              {formatShortcut('⌘U')}
            </span>
          </CommandItem>
          <CommandItem
            onSelect={() =>
              handleSelect(() => {
                toggleEvidence();
              })
            }
          >
            <PanelRight className="mr-2 h-4 w-4 text-muted-foreground" />
            <span>切换右侧证据栏</span>
            <span className="ml-auto font-mono text-xs text-muted-foreground">
              {formatShortcut('⌘\\')}
            </span>
          </CommandItem>
          <CommandItem
            onSelect={() =>
              handleSelect(() => {
                navigate(`/s/${currentSpaceId}/evolve`);
              })
            }
          >
            <Sparkles className="mr-2 h-4 w-4 text-accent-insight" />
            <span>开始一次进化</span>
            <span className="ml-auto font-mono text-xs text-muted-foreground">
              {formatShortcut('⌘4')}
            </span>
          </CommandItem>
        </CommandGroup>

        <CommandSeparator className="my-1" />

        {/* 页面快速跳转 */}
        <CommandGroup heading="跳转到">
          <CommandItem
            onSelect={() =>
              handleSelect(() => {
                navigate(`/s/${currentSpaceId}/chat`);
              })
            }
          >
            <MessageSquarePlus className="mr-2 h-4 w-4 text-muted-foreground" />
            <span>对话问答 (Chat)</span>
            <span className="ml-auto font-mono text-xs text-muted-foreground">
              {formatShortcut('⌘1')}
            </span>
          </CommandItem>
          <CommandItem
            onSelect={() =>
              handleSelect(() => {
                navigate(`/s/${currentSpaceId}/library`);
              })
            }
          >
            <BookOpen className="mr-2 h-4 w-4 text-muted-foreground" />
            <span>知识资料库 (Library)</span>
            <span className="ml-auto font-mono text-xs text-muted-foreground">
              {formatShortcut('⌘2')}
            </span>
          </CommandItem>
          <CommandItem
            onSelect={() =>
              handleSelect(() => {
                navigate(`/s/${currentSpaceId}/memory`);
              })
            }
          >
            <Brain className="mr-2 h-4 w-4 text-muted-foreground" />
            <span>结构化记忆 (Memory)</span>
            <span className="ml-auto font-mono text-xs text-muted-foreground">
              {formatShortcut('⌘3')}
            </span>
          </CommandItem>
          <CommandItem
            onSelect={() =>
              handleSelect(() => {
                navigate(`/s/${currentSpaceId}/expertise`);
              })
            }
          >
            <BarChart3 className="mr-2 h-4 w-4 text-muted-foreground" />
            <span>五维专家度度量 (Expertise)</span>
            <span className="ml-auto font-mono text-xs text-muted-foreground">
              {formatShortcut('⌘5')}
            </span>
          </CommandItem>
        </CommandGroup>

        <CommandSeparator className="my-1" />

        {/* 空间跳转 */}
        <CommandGroup heading="切换知识空间 (Spaces)">
          {spaces.map((s) => (
            <CommandItem
              key={s.id}
              onSelect={() =>
                handleSelect(() => {
                  setCurrentSpaceId(s.id);
                  navigate(`/s/${s.id}/chat`);
                  toast.success(`已切换至空间: ${s.name}`);
                })
              }
            >
              <Layers className="mr-2 h-4 w-4 text-muted-foreground" />
              <div className="flex flex-1 items-center justify-between">
                <span>{s.name}</span>
                <span className="text-[11px] text-muted-foreground">
                  {s.domain}
                </span>
              </div>
            </CommandItem>
          ))}
          <CommandItem
            onSelect={() => handleSelect(() => setSpaceSwitcherOpen(true))}
          >
            <ArrowRight className="mr-2 h-4 w-4 text-muted-foreground" />
            <span>管理所有知识空间...</span>
          </CommandItem>
        </CommandGroup>

        <CommandSeparator className="my-1" />

        {/* 主题设置 */}
        <CommandGroup heading="主题与外观">
          <CommandItem
            onSelect={() =>
              handleSelect(() => {
                setTheme('dark');
                toast.success('已切换为深色沉浸模式');
              })
            }
          >
            <Moon className="mr-2 h-4 w-4" />
            <span>深色模式 (Dark)</span>
            {resolvedTheme === 'dark' && (
              <span className="ml-auto text-xs text-primary font-mono">
                当前
              </span>
            )}
          </CommandItem>
          <CommandItem
            onSelect={() =>
              handleSelect(() => {
                setTheme('light');
                toast.success('已切换为浅色纸质模式');
              })
            }
          >
            <Sun className="mr-2 h-4 w-4" />
            <span>浅色模式 (Light)</span>
            {resolvedTheme === 'light' && (
              <span className="ml-auto text-xs text-primary font-mono">
                当前
              </span>
            )}
          </CommandItem>
          <CommandItem
            onSelect={() =>
              handleSelect(() => {
                setTheme('system');
                toast.success('跟随操作系统外观');
              })
            }
          >
            <Laptop className="mr-2 h-4 w-4" />
            <span>跟随系统 (System)</span>
            {theme === 'system' && (
              <span className="ml-auto text-xs text-primary font-mono">
                当前
              </span>
            )}
          </CommandItem>
        </CommandGroup>

        <CommandSeparator className="my-1" />

        {/* 模型与设置 */}
        <CommandGroup heading="系统设置">
          <CommandItem
            onSelect={() =>
              handleSelect(() => {
                navigate('/settings');
              })
            }
          >
            <Cpu className="mr-2 h-4 w-4 text-muted-foreground" />
            <span>配置模型角色与 Provider 绑定...</span>
          </CommandItem>
        </CommandGroup>
      </CommandList>
    </CmdkDialog>
  );
}
