import { useEffect, useState, type ReactNode } from 'react';
import { NavLink, useLocation, useNavigate } from 'react-router';
import { useSpaceStore } from '@/stores/useSpaceStore';
import { useUiStore } from '@/stores/useUiStore';
import {
  MessageSquare,
  BookOpen,
  Brain,
  Sparkles,
  BarChart3,
  Settings,
  Layers,
  PanelLeftClose,
  PanelLeftOpen,
  ChevronRight,
  ChevronsUpDown,
  Plus,
  Search,
  Pin,
  FileText,
  Globe,
  ClipboardPaste,
  Network,
  Lightbulb,
  Cpu,
  Upload,
  type LucideIcon,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { modKey } from '@/lib/platform';
import { chatService } from '@/lib/api/services/chat';
import { documentService } from '@/lib/api';
import { providerService } from '@/lib/api/services/providers';
import { formatRelativeTime } from '@/lib/time';
import type { components } from '@/lib/api/types.gen';

type Conversation = components['schemas']['Conversation'];
type DocumentItem = components['schemas']['Document'];

interface NavSection {
  label: string;
  route: string;
  icon: LucideIcon;
  shortcut: string;
  /** 可展开出卡片列表的分组。 */
  group?: 'chat' | 'library' | 'memory';
}

const sections: NavSection[] = [
  { label: '知识对话', route: 'chat', icon: MessageSquare, shortcut: '1', group: 'chat' },
  { label: '资料库', route: 'library', icon: BookOpen, shortcut: '2', group: 'library' },
  { label: '知识记忆', route: 'memory', icon: Brain, shortcut: '3', group: 'memory' },
  { label: '进化中心', route: 'evolve', icon: Sparkles, shortcut: '4' },
  { label: '专家评测', route: 'expertise', icon: BarChart3, shortcut: '5' },
  { label: '知识图谱', route: 'graph', icon: Network, shortcut: '6' },
];

const RECENT_LIMIT = 6;


/** 本机推理服务的地址特征：用来在侧栏底部如实标出「本地 / 云端」。 */
function isLocalEndpoint(baseUrl?: string | null, adapter?: string | null): boolean {
  if (adapter && /ollama|sentence_transformers|lmstudio/i.test(adapter)) return true;
  return Boolean(baseUrl && /localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]/.test(baseUrl));
}

function docIcon(doc: DocumentItem): LucideIcon {
  if (doc.source_type === 'url') return Globe;
  if (doc.source_type === 'paste') return ClipboardPaste;
  return FileText;
}

const STATUS_DOT: Record<string, string> = {
  ready: 'bg-accent-insight',
  failed: 'bg-destructive',
};

/**
 * 侧栏里展开的卡片列表。
 *
 * 一级导航只告诉你「有哪些功能」，真正常用的是「刚才那段对话」「刚导入的那份资料」。
 * 展开后直接列出最近的条目，每条是一张轻卡片：标题 + 一行元信息，当前打开的那条高亮——
 * 与桌面端会话列表的用法一致，不必先进页面再找。
 */
function CardList({ children, footer }: { children: ReactNode; footer?: ReactNode }) {
  return (
    <div className="ml-[22px] mt-1 mb-2 border-l border-sidebar-border pl-2">
      <ul className="list-card-in space-y-1">{children}</ul>
      {footer}
    </div>
  );
}

function ListCard({
  active,
  onClick,
  icon: Icon,
  title,
  meta,
  badge,
}: {
  active?: boolean;
  onClick: () => void;
  icon?: LucideIcon;
  title: string;
  meta?: ReactNode;
  badge?: ReactNode;
}) {
  return (
    <li>
      <button
        type="button"
        onClick={onClick}
        aria-current={active ? 'true' : undefined}
        className={cn(
          'group flex w-full items-start gap-2 rounded-lg border px-2.5 py-[7px] text-left transition-all duration-150',
          active
            ? 'border-border bg-card shadow-[var(--shadow-card)]'
            : 'border-transparent hover:border-sidebar-border hover:bg-card/70',
        )}
      >
        {Icon && (
          <Icon
            className={cn(
              'mt-[3px] h-3.5 w-3.5 shrink-0',
              active ? 'text-primary' : 'text-muted-foreground',
            )}
          />
        )}
        <span className="min-w-0 flex-1">
          <span
            className={cn(
              'block truncate text-[12.5px] leading-snug',
              active ? 'font-medium text-foreground' : 'text-foreground/85',
            )}
          >
            {title}
          </span>
          {meta && (
            <span className="mt-0.5 flex items-center gap-1.5 truncate text-[11px] leading-tight text-muted-foreground">
              {meta}
            </span>
          )}
        </span>
        {badge}
      </button>
    </li>
  );
}

function ListFooterLink({ onClick, children }: { onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="mt-1 flex w-full items-center gap-1 rounded-md px-2.5 py-1 text-[11.5px] text-muted-foreground transition-colors hover:text-foreground"
    >
      {children}
      <ChevronRight className="h-3 w-3" />
    </button>
  );
}

interface SidebarProps {
  /** 手机端从左侧滑出的抽屉：始终展开、占满抽屉宽度、没有收起按钮。 */
  variant?: 'rail' | 'drawer';
}

export function Sidebar({ variant = 'rail' }: SidebarProps) {
  const isDrawer = variant === 'drawer';
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const { currentSpaceId, getCurrentSpace } = useSpaceStore();
  const {
    setSpaceSwitcherOpen,
    setCommandOpen,
    isSidebarExpanded: railExpanded,
    toggleSidebar,
    activeConversationId,
    conversationsVersion,
    expandedNavGroups,
    toggleNavGroup,
  } = useUiStore();
  const space = getCurrentSpace();
  const expanded = isDrawer || railExpanded;
  const labelClass = isDrawer ? 'block min-w-0' : expanded ? 'hidden min-w-0 md:block' : 'hidden';
  const showLists = expanded;

  const [conversations, setConversations] = useState<Conversation[] | null>(null);
  const [conversationTotal, setConversationTotal] = useState(0);
  const [documents, setDocuments] = useState<DocumentItem[] | null>(null);
  const [documentTotal, setDocumentTotal] = useState(0);
  const [chatModel, setChatModel] = useState<{ name: string; local: boolean } | null>(null);

  const chatOpen = expandedNavGroups.includes('chat');
  const libraryOpen = expandedNavGroups.includes('library');

  useEffect(() => {
    if (!currentSpaceId || !showLists || !chatOpen) return;
    const controller = new AbortController();
    chatService
      .getConversations(currentSpaceId, { limit: RECENT_LIMIT, signal: controller.signal })
      .then((page) => {
        setConversations(page.items);
        setConversationTotal(page.total);
      })
      .catch(() => {
        if (!controller.signal.aborted) setConversations([]);
      });
    return () => controller.abort();
  }, [currentSpaceId, showLists, chatOpen, conversationsVersion]);

  useEffect(() => {
    if (!currentSpaceId || !showLists || !libraryOpen) return;
    const controller = new AbortController();
    documentService
      .getDocuments(currentSpaceId, { limit: 5, signal: controller.signal })
      .then((page) => {
        setDocuments(page.items as DocumentItem[]);
        setDocumentTotal(page.total);
      })
      .catch(() => {
        if (!controller.signal.aborted) setDocuments([]);
      });
    return () => controller.abort();
    // 换页回来重拉一次：资料库页里刚导入、刚删掉的条目要反映到这里
  }, [currentSpaceId, showLists, libraryOpen, pathname, space?.doc_count]);

  useEffect(() => {
    let alive = true;
    Promise.all([providerService.getRoleBindings(), providerService.getProviders()])
      .then(([roles, providers]) => {
        if (!alive) return;
        const bound = providers.find((provider) => provider.id === roles.chat);
        if (!bound) return;
        setChatModel({
          name: bound.model || bound.id,
          local: isLocalEndpoint(bound.base_url, bound.adapter),
        });
      })
      .catch(() => {
        // 取不到就不显示，模型状态另有顶栏告警
      });
    return () => {
      alive = false;
    };
  }, [pathname]);

  const spaceBase = currentSpaceId ? `/s/${currentSpaceId}` : '';

  const renderGroup = (group: NavSection['group']) => {
    if (!showLists || !currentSpaceId || !group || !expandedNavGroups.includes(group)) return null;

    if (group === 'chat') {
      if (conversations === null) return null;
      return (
        <CardList
          footer={
            conversationTotal > RECENT_LIMIT ? (
              <ListFooterLink onClick={() => navigate(`${spaceBase}/chat?history=1`)}>
                全部 {conversationTotal} 段对话
              </ListFooterLink>
            ) : null
          }
        >
          {conversations.length === 0 && (
            <li className="px-2.5 py-1.5 text-[11.5px] text-muted-foreground">还没有对话</li>
          )}
          {conversations.map((conversation) => (
            <ListCard
              key={conversation.id}
              active={pathname.endsWith('/chat') && conversation.id === activeConversationId}
              onClick={() => navigate(`${spaceBase}/chat?c=${conversation.id}`)}
              title={conversation.title}
              meta={formatRelativeTime(conversation.updated_at)}
              badge={
                conversation.pinned ? (
                  <Pin className="mt-[3px] h-3 w-3 shrink-0 text-muted-foreground" aria-label="已置顶" />
                ) : null
              }
            />
          ))}
        </CardList>
      );
    }

    if (group === 'library') {
      if (documents === null) return null;
      return (
        <CardList
          footer={
            <ListFooterLink onClick={() => navigate(`${spaceBase}/library`)}>
              {documentTotal > documents.length ? `全部 ${documentTotal} 份资料` : '管理资料'}
            </ListFooterLink>
          }
        >
          {documents.length === 0 && (
            <ListCard
              icon={Upload}
              onClick={() => navigate(`${spaceBase}/library?import=1`)}
              title="导入第一份资料"
              meta="PDF、Markdown、网页或粘贴文本"
            />
          )}
          {documents.map((doc) => (
            <ListCard
              key={doc.id}
              icon={docIcon(doc)}
              onClick={() =>
                useUiStore.getState().openReader({ documentId: doc.id, documentTitle: doc.title })
              }
              title={doc.title}
              meta={
                <>
                  <span
                    className={cn(
                      'h-1.5 w-1.5 shrink-0 rounded-full',
                      STATUS_DOT[doc.status] ?? 'bg-accent-ai animate-pulse',
                    )}
                  />
                  <span className="truncate">
                    {doc.status === 'ready'
                      ? formatRelativeTime(doc.updated_at)
                      : doc.status === 'failed'
                        ? '处理失败'
                        : '处理中'}
                  </span>
                </>
              }
            />
          ))}
        </CardList>
      );
    }

    const memoryTab = new URLSearchParams(window.location.search).get('tab') || 'cards';
    const onMemory = pathname.endsWith('/memory');
    return (
      <CardList>
        <ListCard
          icon={Layers}
          active={onMemory && memoryTab === 'cards'}
          onClick={() => navigate(`${spaceBase}/memory?tab=cards`)}
          title="知识卡片"
          meta="从资料里提炼的概念与事实"
        />
        <ListCard
          icon={Network}
          active={onMemory && memoryTab === 'graph'}
          onClick={() => navigate(`${spaceBase}/memory?tab=graph`)}
          title="知识图谱"
          meta="实体与关系"
        />
        <ListCard
          icon={Lightbulb}
          active={onMemory && memoryTab === 'insights'}
          onClick={() => navigate(`${spaceBase}/memory?tab=insights`)}
          title="经验库"
          meta={
            typeof space?.insight_count === 'number'
              ? `${space.insight_count} 条已沉淀的经验`
              : '从反馈里沉淀的方法'
          }
        />
      </CardList>
    );
  };

  return (
    <aside
      aria-label="工作空间导航"
      className={cn(
        'app-sidebar h-full shrink-0 flex-col bg-sidebar',
        isDrawer
          ? 'flex w-full'
          : cn(
              'hidden w-[64px] border-r border-sidebar-border transition-[width] duration-200 md:flex',
              expanded && 'md:w-[264px]',
            ),
      )}
    >
      {/* 品牌与收起 */}
      <div className="flex h-14 shrink-0 items-center gap-2.5 px-4">
        <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-primary text-primary-foreground">
          <Layers className="h-4 w-4" />
        </span>
        <span className={cn('flex-1 font-display text-[17px] text-foreground', labelClass)}>
          AgentMem
        </span>
        <button
          type="button"
          onClick={toggleSidebar}
          aria-label={expanded ? '收起侧边栏' : '展开侧边栏'}
          aria-expanded={expanded}
          title={`${expanded ? '收起' : '展开'}侧边栏（${modKey}B）`}
          className={cn(
            'hidden h-7 w-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-sidebar-accent hover:text-foreground',
            expanded && !isDrawer ? 'md:flex' : '',
          )}
        >
          <PanelLeftClose className="h-4 w-4" />
        </button>
      </div>

      {/* 新对话 / 搜索 */}
      <div className="space-y-0.5 px-3 pb-2">
        <button
          type="button"
          onClick={() => currentSpaceId && navigate(`${spaceBase}/chat?new=1`)}
          title={`新对话（${modKey}N）`}
          aria-label="新对话"
          className="flex h-9 w-full items-center gap-2.5 rounded-lg px-2.5 text-[13.5px] text-foreground transition-colors hover:bg-sidebar-accent"
        >
          <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-primary/12 text-primary">
            <Plus className="h-3.5 w-3.5" />
          </span>
          <span className={cn('flex-1 text-left font-medium', labelClass)}>新对话</span>
          <kbd className={cn('font-mono text-[10px] text-muted-foreground', labelClass)}>
            {modKey}N
          </kbd>
        </button>
        <button
          type="button"
          onClick={() => setCommandOpen(true)}
          title={`搜索知识或命令（${modKey}K）`}
          aria-label="搜索"
          className="flex h-9 w-full items-center gap-2.5 rounded-lg px-2.5 text-[13.5px] text-muted-foreground transition-colors hover:bg-sidebar-accent hover:text-foreground"
        >
          <Search className="h-4 w-4 shrink-0 mx-0.5" />
          <span className={cn('flex-1 text-left', labelClass)}>搜索</span>
          <kbd className={cn('font-mono text-[10px]', labelClass)}>{modKey}K</kbd>
        </button>
      </div>

      <nav aria-label="主要功能" className="min-h-0 flex-1 overflow-y-auto px-3 pb-3">
        {sections.map(({ label, route, icon: Icon, shortcut: key, group }) => {
          const groupOpen = Boolean(group && expandedNavGroups.includes(group));
          return (
            <div key={route} className="mt-0.5">
              <div className="group/nav relative flex items-center">
                <NavLink
                  to={currentSpaceId ? `${spaceBase}/${route}` : '/'}
                  aria-label={label}
                  title={`${label}（${modKey}${key}）`}
                  end
                  className={({ isActive }) =>
                    cn(
                      'sidebar-link relative flex h-9 flex-1 items-center gap-2.5 rounded-lg px-2.5 text-[13.5px] transition-colors',
                      isActive && currentSpaceId
                        ? 'bg-sidebar-accent font-medium text-foreground'
                        : 'text-foreground/75 hover:bg-sidebar-accent/70 hover:text-foreground',
                    )
                  }
                >
                  {({ isActive }) => (
                    <>
                      <Icon
                        className={cn(
                          'h-4 w-4 shrink-0 mx-0.5',
                          isActive && currentSpaceId ? 'text-primary' : 'text-muted-foreground',
                        )}
                      />
                      <span className={cn('flex-1', labelClass)}>{label}</span>
                      <kbd
                        className={cn(
                          'font-mono text-[10px] text-muted-foreground transition-opacity',
                          labelClass,
                          isDrawer && 'hidden',
                          group && expanded && !isDrawer && 'md:group-hover/nav:opacity-0',
                        )}
                      >
                        {modKey}
                        {key}
                      </kbd>
                    </>
                  )}
                </NavLink>
                {group && expanded && (
                  <button
                    type="button"
                    onClick={() => toggleNavGroup(group)}
                    aria-expanded={groupOpen}
                    aria-label={`${groupOpen ? '收起' : '展开'}${label}列表`}
                    className={cn(
                      'absolute right-1.5 h-7 w-7 items-center justify-center rounded-md text-muted-foreground transition-all hover:bg-card hover:text-foreground',
                      isDrawer ? 'flex' : 'hidden md:flex',
                      groupOpen || isDrawer
                        ? 'opacity-100'
                        : 'opacity-0 group-hover/nav:opacity-100 focus-visible:opacity-100',
                    )}
                  >
                    <ChevronRight
                      className={cn('h-3.5 w-3.5 transition-transform duration-200', groupOpen && 'rotate-90')}
                    />
                  </button>
                )}
              </div>
              {renderGroup(group)}
            </div>
          );
        })}
      </nav>

      {/* 底部：当前空间卡片、模型状态与设置 */}
      <div className="space-y-1.5 border-t border-sidebar-border p-3">
        {expanded && chatModel && (
          <button
            type="button"
            onClick={() => navigate('/settings')}
            title={`对话角色当前绑定 ${chatModel.name}，点击去「设置 → 模型」改绑`}
            className={cn(
              'flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-[11.5px] text-muted-foreground transition-colors hover:bg-sidebar-accent hover:text-foreground',
              labelClass,
            )}
          >
            <span className="flex w-full items-center gap-2">
              <Cpu className="h-3.5 w-3.5 shrink-0" />
              <span className="min-w-0 flex-1 truncate text-left font-mono">{chatModel.name}</span>
              <span
                className={cn(
                  'shrink-0 rounded-full px-1.5 py-px text-[10px] font-medium',
                  chatModel.local
                    ? 'bg-accent-insight/12 text-accent-insight'
                    : 'bg-muted text-muted-foreground',
                )}
              >
                {chatModel.local ? '本地' : '云端'}
              </span>
            </span>
          </button>
        )}
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => setSpaceSwitcherOpen(true)}
            aria-label="切换知识空间"
            title="切换知识空间"
            className={cn(
              'flex min-w-0 flex-1 items-center gap-2.5 rounded-lg p-1.5 text-left transition-colors hover:bg-sidebar-accent',
            )}
          >
            <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-border bg-card font-display text-[14px] text-primary shadow-[var(--shadow-card)]">
              {(space?.name || '空').slice(0, 1)}
            </span>
            <span className={cn('min-w-0 flex-1', labelClass)}>
              <span className="block truncate text-[13px] font-medium text-foreground">
                {space?.name || '创建你的第一个空间'}
              </span>
              <span className="block truncate text-[11px] text-muted-foreground">
                {space
                  ? `${space.doc_count ?? 0} 份资料${typeof space.expertise_overall === 'number' ? ` · 专家度 ${space.expertise_overall.toFixed(0)}` : ''}`
                  : '知识空间'}
              </span>
            </span>
            <ChevronsUpDown className={cn('h-3.5 w-3.5 shrink-0 text-muted-foreground', labelClass)} />
          </button>
          <NavLink
            to="/settings"
            aria-label="系统设置"
            title="系统设置"
            className={({ isActive }) =>
              cn(
                'flex h-8 w-8 shrink-0 items-center justify-center rounded-lg transition-colors',
                isActive
                  ? 'bg-sidebar-accent text-foreground'
                  : 'text-muted-foreground hover:bg-sidebar-accent hover:text-foreground',
                !expanded && 'hidden',
              )
            }
          >
            <Settings className="h-4 w-4" />
          </NavLink>
        </div>
        {!expanded && (
          <div className="flex flex-col items-center gap-1">
            <NavLink
              to="/settings"
              aria-label="系统设置"
              title="系统设置"
              className="flex h-9 w-9 items-center justify-center rounded-lg text-muted-foreground hover:bg-sidebar-accent hover:text-foreground"
            >
              <Settings className="h-4 w-4" />
            </NavLink>
            <button
              type="button"
              onClick={toggleSidebar}
              aria-label="展开侧边栏"
              aria-expanded={false}
              title={`展开侧边栏（${modKey}B）`}
              className="hidden h-9 w-9 items-center justify-center rounded-lg text-muted-foreground hover:bg-sidebar-accent hover:text-foreground md:flex"
            >
              <PanelLeftOpen className="h-4 w-4" />
            </button>
          </div>
        )}
      </div>
    </aside>
  );
}
