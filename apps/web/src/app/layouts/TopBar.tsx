import React, { useEffect, useState } from 'react';
import { Sidebar } from './Sidebar';
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from '@/components/ui/sheet';
import { useLocation, useNavigate } from 'react-router';
import { expertiseService } from '@/lib/api/services/expertise';
import { useSpaceStore } from '@/stores/useSpaceStore';
import { useUiStore } from '@/stores/useUiStore';
import { useThemeStore } from '@/stores/useThemeStore';
import { ConfidenceRing } from '@/components/shared/ConfidenceRing';
import { KeyboardShortcutHint } from '@/components/shared/KeyboardShortcutHint';
import { ProviderAlertBadge } from '@/components/shared/ProviderAlertBadge';
import { Button } from '@/components/ui/button';
import {
  ChevronDown,
  Command,
  Sun,
  Moon,
  Settings,
  Sparkles,
  ShieldCheck,
  ChevronRight,
  Menu,
} from 'lucide-react';

export function TopBar() {
  const navigate = useNavigate();
  const { pathname, search } = useLocation();
  // 手机端没有常驻侧栏：导航收进左上角的抽屉，换页即收起
  const [drawerOpen, setDrawerOpen] = useState(false);
  useEffect(() => {
    setDrawerOpen(false);
  }, [pathname, search]);
  const { getCurrentSpace } = useSpaceStore();
  const { setSpaceSwitcherOpen, setCommandOpen } = useUiStore();
  const { setTheme, resolvedTheme } = useThemeStore();
  const space = getCurrentSpace();
  const sectionName =
    pathname === '/settings'
      ? '系统设置'
      : (
          {
            chat: '知识对话',
            library: '资料库',
            memory: '知识记忆',
            evolve: '进化中心',
            expertise: '专家评测',
            graph: '知识图谱',
          } as Record<string, string>
        )[pathname.split('/').pop() || ''] || '工作空间';

  // `space.expertise_overall` 是最近一张**已落库的快照**（上次进化时写的），
  // 而专家度页展示的是实时计算值。两者能差好几分——顶栏挂着旧快照、页面显示新分数，
  // 同一个「专家度」就有了两个值。这里改取实时值，让全应用只有一个口径。
  // `/expertise` 是纯 SQL 聚合、不触发任何模型调用，按路由变化重取足够便宜，
  // 也顺带让「刷新度量」之后回到别的页时徽章是新的。
  //
  // 取实时值的请求可能排在页面自己的一堆请求后面（记忆页一进来就要拉上百张卡片），
  // 这几秒里不能退回快照：实测记忆页顶栏显示 44.3、别的页显示 64.0，同一时刻两个数。
  // 所以换页时保留上一次的实时值，只有实时接口真的失败才用快照，并在提示里说明。
  const [live, setLive] = useState<{
    spaceId: string;
    overall: number | null;
    failed: boolean;
  } | null>(null);
  const spaceId = space?.id;

  useEffect(() => {
    if (!spaceId) {
      setLive(null);
      return;
    }
    let cancelled = false;
    expertiseService
      .getExpertise(spaceId)
      .then((score) => {
        if (!cancelled)
          setLive({ spaceId, overall: score.overall, failed: false });
      })
      .catch(() => {
        if (!cancelled) setLive({ spaceId, overall: null, failed: true });
      });
    return () => {
      cancelled = true;
    };
  }, [spaceId, pathname]);

  const liveForSpace = live && live.spaceId === spaceId ? live : null;
  const usingSnapshot = liveForSpace?.failed ?? false;
  const overall = liveForSpace
    ? usingSnapshot
      ? (space?.expertise_overall ?? null)
      : liveForSpace.overall
    : null;
  const badgeText = !liveForSpace
    ? '…'
    : overall != null
      ? overall.toFixed(1)
      : '待评';

  // 严格基于当前实际生效的 resolvedTheme 切换，彻底避免系统偏好下的假切换与空点
  const toggleTheme = () => {
    setTheme(resolvedTheme === 'dark' ? 'light' : 'dark');
  };

  return (
    <header className="flex h-12 w-full shrink-0 items-center justify-between gap-3 border-b border-border/70 bg-background/85 px-2 backdrop-blur-md sm:px-5">
      <Sheet open={drawerOpen} onOpenChange={setDrawerOpen}>
        <SheetContent
          side="left"
          showCloseButton={false}
          className="gap-0 p-0 data-[side=left]:w-[min(300px,86vw)]"
        >
          <SheetHeader className="sr-only">
            <SheetTitle>导航</SheetTitle>
            <SheetDescription>切换功能、打开最近的对话与资料</SheetDescription>
          </SheetHeader>
          <Sidebar variant="drawer" />
        </SheetContent>
      </Sheet>
      {/* Space 切换器与专家度徽章 */}
      <div className="flex min-w-0 items-center gap-1.5 sm:gap-3">
        <Button
          variant="ghost"
          size="icon"
          onClick={() => setDrawerOpen(true)}
          aria-label="打开导航"
          className="h-9 w-9 text-muted-foreground md:hidden"
        >
          <Menu className="h-5 w-5" />
        </Button>
        <button
          type="button"
          onClick={() => setSpaceSwitcherOpen(true)}
          aria-label="切换当前知识空间"
          className="group flex min-w-0 items-center gap-1.5 rounded-lg px-2 py-1 text-[13px] font-medium text-foreground transition-colors hover:bg-muted"
        >
          <div className="flex h-4.5 w-4.5 items-center justify-center rounded-sm bg-primary/10 text-primary">
            <ShieldCheck className="h-3.5 w-3.5" />
          </div>
          <span className="max-w-[130px] truncate font-medium tracking-tight sm:max-w-[200px]">
            {space?.name || '选择/创建空间'}
          </span>
          <ChevronDown className="h-3.5 w-3.5 text-muted-foreground transition-transform group-hover:translate-y-0.5" />
        </button>

        <div className="hidden items-center gap-3 text-xs text-muted-foreground lg:flex">
          <ChevronRight className="h-3.5 w-3.5" />
          <span>{sectionName}</span>
        </div>

        {/* 专家度指数徽章 */}
        {space && (
          <button
            type="button"
            onClick={() => navigate(`/s/${space.id}/expertise`)}
            className="hidden items-center gap-1.5 rounded-full sm:flex border border-border bg-muted/30 px-2 py-0.5 text-xs text-muted-foreground transition-colors hover:border-accent-insight/40 hover:text-foreground"
            title={
              usingSnapshot
                ? '实时专家度暂时取不到，这里显示的是上次进化时的快照。点击查看详情'
                : '点击查看五维专家度评测与成长曲线'
            }
          >
            <ConfidenceRing
              value={overall || 0}
              max={100}
              size={18}
              strokeWidth={2.5}
              showText={false}
            />
            <span className="font-mono text-[11px] font-medium text-foreground">
              {badgeText}
            </span>
            <span className="text-[10px] text-muted-foreground">专家度</span>
          </button>
        )}
      </div>

      {/* 右侧工具栏: provider 告警、⌘K、主题切换、设置 */}
      <div className="flex shrink-0 items-center gap-1.5 sm:gap-2">
        {/* 有模型连不上时才出现——常驻一个「一切正常」只会变成背景噪音 */}
        <ProviderAlertBadge />

        {/* ⌘K 唤起搜索栏 */}
        <button
          type="button"
          onClick={() => setCommandOpen(true)}
          aria-label="搜索知识或命令"
          className="flex h-8 items-center gap-2 rounded-lg border border-border bg-background/60 px-2.5 text-xs text-muted-foreground transition-colors hover:border-primary/40 hover:text-foreground"
        >
          <Command className="h-3.5 w-3.5 text-muted-foreground" />
          <span className="hidden xl:inline">搜索知识或命令</span>
          <span className="hidden sm:inline">
            <KeyboardShortcutHint shortcut="⌘K" />
          </span>
        </button>

        {/* 快速进入进化页 */}
        {space && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => navigate(`/s/${space.id}/evolve`)}
            className="hidden lg:flex h-8 items-center gap-1 text-xs text-accent-insight hover:bg-accent-insight/10"
          >
            <Sparkles className="h-3.5 w-3.5" />
            <span>进化</span>
          </Button>
        )}

        {/* 主题切换 */}
        <Button
          variant="ghost"
          size="icon"
          onClick={toggleTheme}
          className="h-8 w-8 text-muted-foreground hover:text-foreground cursor-pointer"
          aria-label={
            resolvedTheme === 'dark' ? '切换浅色主题' : '切换深色主题'
          }
          title={resolvedTheme === 'dark' ? '切换浅色主题' : '切换深色主题'}
        >
          {resolvedTheme === 'dark' ? (
            <Sun className="h-4 w-4" />
          ) : (
            <Moon className="h-4 w-4" />
          )}
        </Button>

        {/* 系统设置 */}
        <Button
          variant="ghost"
          size="icon"
          onClick={() => navigate('/settings')}
          className="h-8 w-8 text-muted-foreground hover:text-foreground"
          aria-label="系统与模型配置"
          title="系统与模型配置"
        >
          <Settings className="h-4 w-4" />
        </Button>
      </div>
    </header>
  );
}
