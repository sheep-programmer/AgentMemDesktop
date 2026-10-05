import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router';
import { useSpaceStore } from '@/stores/useSpaceStore';
import { useUiStore } from '@/stores/useUiStore';
import { Button } from '@/components/ui/button';
import { Database, Plus, Loader2 } from 'lucide-react';

/**
 * 应用入口：把「打开应用」这件事解析成「打开哪个 Space」。
 *
 * 之前这里是个写死的跳转目标（`/s/space-drug-discovery/chat`，来自前端 mock 数据），
 * 对真实后端来说那个 id 根本不存在：顶部栏因为拿到了真实的 Space 列表而显示正常，
 * 内容区却在查一个不存在的 Space，于是每个页面都停在「加载失败 / 全 0」。
 * 入口必须按后端返回的列表来决定去哪，而不是按 mock 数据。
 */
export function SpaceEntry() {
  const navigate = useNavigate();
  const { spaces, currentSpaceId, isLoading, loadSpaces, loadError } = useSpaceStore();
  const setSpaceSwitcherOpen = useUiStore((state) => state.setSpaceSwitcherOpen);
  const [resolved, setResolved] = useState(false);

  useEffect(() => {
    // loadSpaces 会把 currentSpaceId 落在一个真实存在的 Space 上（没有就不动）
    void loadSpaces().finally(() => setResolved(true));
  }, [loadSpaces]);

  useEffect(() => {
    if (!resolved || isLoading) return;
    if (spaces.length > 0 && currentSpaceId) {
      navigate(`/s/${currentSpaceId}/chat`, { replace: true });
    }
  }, [resolved, isLoading, spaces, currentSpaceId, navigate]);

  if (!resolved || isLoading || (spaces.length > 0 && currentSpaceId)) {
    return (
      <div className="flex h-screen w-full items-center justify-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" />
        <span>正在加载知识空间…</span>
      </div>
    );
  }

  // 列表拉取失败：**不能**当成「一个 Space 都没有」。这是启动后第一眼看到的页面，
  // 后端没起来（或注册表出问题）时，原本会直截了当告诉用户「还没有知识空间，
  // 先建一个开始」——而他的知识库可能好端端地在磁盘上。引导去新建只会越走越错。
  if (loadError) {
    return (
      <div className="flex h-screen w-full flex-col items-center justify-center gap-4 p-6 text-center">
        <div className="flex h-12 w-12 items-center justify-center rounded-full bg-destructive/10 text-destructive">
          <Database className="h-5 w-5" />
        </div>
        <div className="space-y-1">
          <div className="text-base font-semibold text-foreground">无法获取知识空间列表</div>
          <p className="max-w-md text-xs leading-relaxed text-muted-foreground">
            后端可能没有启动。请先确认 <span className="font-mono">uv run agentmem serve</span>{' '}
            正在运行，再重试——<b>这不代表你的知识库没了</b>，数据仍在本地磁盘上，
            先别急着新建以免重复。
          </p>
        </div>
        <Button size="sm" className="gap-1.5" onClick={() => void loadSpaces()}>
          重试
        </Button>
      </div>
    );
  }

  // 一个 Space 都没有：这是第一次打开应用的样子，直接引导建一个，
  // 而不是把用户丢进一个查不到数据的空壳页面
  return (
    <div className="flex h-screen w-full flex-col items-center justify-center gap-4 p-6 text-center">
      <div className="flex h-12 w-12 items-center justify-center rounded-full bg-primary/10 text-primary">
        <Database className="h-5 w-5" />
      </div>
      <div className="space-y-1">
        <div className="text-base font-semibold text-foreground">还没有知识空间</div>
        <p className="max-w-md text-xs leading-relaxed text-muted-foreground">
          一个知识空间就是一位领域专家：导入资料、提问、纠正它，它会把这些沉淀成可复用的经验。
          先建一个开始，或用 <span className="font-mono">uv run agentmem demo</span> 铺一个带数据的示例。
        </p>
      </div>
      <Button size="sm" className="gap-1.5" onClick={() => setSpaceSwitcherOpen(true)}>
        <Plus className="h-3.5 w-3.5" />
        新建知识空间
      </Button>
    </div>
  );
}
