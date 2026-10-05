import React, { useState } from 'react';
import { useNavigate } from 'react-router';
import { spaceService } from '@/lib/api/services/spaces';
import { useSpaceStore } from '@/stores/useSpaceStore';
import { useUiStore } from '@/stores/useUiStore';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { ConfidenceRing } from '@/components/shared/ConfidenceRing';
import { ShieldAlert, Sparkles, Scale, Check, Plus, FolderKanban, Pencil } from 'lucide-react';
import { toast } from 'sonner';

export function SpaceSwitcherDialog() {
  const navigate = useNavigate();
  const { spaces, currentSpaceId, setCurrentSpaceId, createSpace, loadError } = useSpaceStore();
  const { isSpaceSwitcherOpen, setSpaceSwitcherOpen } = useUiStore();
  const [isCreating, setIsCreating] = useState(false);
  const [newName, setNewName] = useState('');
  const [newDomain, setNewDomain] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);

  const getSpaceIcon = (icon?: string | null) => {
    switch (icon) {
      case 'ShieldAlert':
        return <ShieldAlert className="h-5 w-5 text-primary" />;
      case 'Sparkles':
        return <Sparkles className="h-5 w-5 text-accent-ai" />;
      case 'Scale':
        return <Scale className="h-5 w-5 text-accent-warn" />;
      default:
        return <FolderKanban className="h-5 w-5 text-muted-foreground" />;
    }
  };

  const handleSelectSpace = (spaceId: string) => {
    setCurrentSpaceId(spaceId);
    setSpaceSwitcherOpen(false);
    navigate(`/s/${spaceId}/chat`);
  };

  const [editingId, setEditingId] = useState<string | null>(null);
  const [editName, setEditName] = useState('');
  const [editDomain, setEditDomain] = useState('');

  const startEdit = (space: { id: string; name: string; domain: string }) => {
    setEditingId(space.id);
    setEditName(space.name);
    setEditDomain(space.domain);
  };

  const handleSaveSpace = async (e: React.FormEvent, spaceId: string) => {
    e.preventDefault();
    if (!editName.trim() || !editDomain.trim()) return;
    try {
      const updated = await spaceService.updateSpace(spaceId, {
        name: editName.trim(),
        domain: editDomain.trim(),
      });
      useSpaceStore.getState().setSpaces(
        spaces.map((item) => (item.id === spaceId ? { ...item, ...updated } : item)),
      );
      setEditingId(null);
      toast.success('已保存；领域变了记得重新生成领域大纲');
    } catch (err: unknown) {
      toast.error(`保存失败：${(err as Error)?.message || '网络错误'}`);
    }
  };

  const handleCreateSpace = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newName.trim()) return;

    setIsSubmitting(true);
    try {
      // 主题说明选填：不填就用空间名称，之后随时能在专家度页或这里改
      const created = await createSpace({
        name: newName.trim(),
        domain: newDomain.trim() || newName.trim(),
      });
      toast.success(`空间「${created.name}」已创建`);
      setNewName('');
      setNewDomain('');
      setIsCreating(false);
      handleSelectSpace(created.id);
    } catch (err: unknown) {
      toast.error(`创建失败: ${(err as Error)?.message || '网络错误'}`);
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <Dialog open={isSpaceSwitcherOpen} onOpenChange={setSpaceSwitcherOpen}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="text-base font-semibold">切换知识空间</DialogTitle>
        </DialogHeader>

        {isCreating ? (
          <form onSubmit={handleCreateSpace} className="mt-2 space-y-3 text-xs">
            <div>
              <label className="font-medium text-foreground">空间名称</label>
              <input
                type="text"
                required
                placeholder="例如: 校园办事助手"
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                className="mt-1 w-full rounded-md border border-input bg-background px-3 py-1.5 text-xs outline-none"
              />
            </div>
            <div>
              <label className="font-medium text-foreground">
                这个空间是关于什么的？<span className="font-normal text-muted-foreground">（选填）</span>
              </label>
              <input
                type="text"
                placeholder="例如: 高校教务、学籍与校园生活事务"
                value={newDomain}
                onChange={(e) => setNewDomain(e.target.value)}
                className="mt-1 w-full rounded-md border border-input bg-background px-3 py-1.5 text-xs outline-none"
              />
              <p className="mt-1 text-[10px] leading-relaxed text-muted-foreground">
                写得越具体，AI 整理领域大纲、出测验题时越准；不填就按空间名称理解。
              </p>
            </div>
            <div className="flex justify-end gap-2 pt-2 border-t border-border/50">
              <Button type="button" variant="ghost" size="sm" onClick={() => setIsCreating(false)}>
                返回
              </Button>
              <Button type="submit" size="sm" disabled={isSubmitting}>
                {isSubmitting ? '创建中...' : '确认创建'}
              </Button>
            </div>
          </form>
        ) : (
          <>
            <div className="mt-2 space-y-2 max-h-72 overflow-y-auto">
              {spaces.length === 0 ? (
                /* 「取不到」和「真的没有」必须分开说。空列表原本一律显示「还没有知识空间，
                   请新建」——而拉取失败时也是空列表，那句话会把用户推去建一个新的，
                   哪怕他的 Space 好端端地在磁盘上（注册表丢行事故后看到的正是这一幕）。 */
                <div className="p-4 text-center text-xs text-muted-foreground">
                  {loadError
                    ? '无法获取知识空间列表，请检查后端是否在运行；先不要新建，以免和已有空间重复。'
                    : '当前还没有知识空间，请点击下方「新建知识空间」进行创建。'}
                </div>
              ) : (
                spaces.map((space) => {
                  const isSelected = space.id === currentSpaceId;
                  if (editingId === space.id) {
                    return (
                      <form
                        key={space.id}
                        onSubmit={(e) => handleSaveSpace(e, space.id)}
                        onClick={(e) => e.stopPropagation()}
                        className="space-y-2 rounded-lg border border-primary/40 bg-primary/5 p-3"
                      >
                        <input
                          type="text"
                          value={editName}
                          onChange={(e) => setEditName(e.target.value)}
                          placeholder="空间名称"
                          className="w-full rounded-md border border-input bg-background px-2.5 py-1.5 text-xs text-foreground outline-none"
                        />
                        <input
                          type="text"
                          value={editDomain}
                          onChange={(e) => setEditDomain(e.target.value)}
                          placeholder="领域（写具体些，例如：高校教务与学籍管理）"
                          className="w-full rounded-md border border-input bg-background px-2.5 py-1.5 text-xs text-foreground outline-none"
                        />
                        <p className="text-[10px] leading-relaxed text-muted-foreground">
                          AI 按这里的描述理解你的资料；改完后到专家度页点「刷新盲区」，领域大纲会按新描述重新整理。
                        </p>
                        <div className="flex justify-end gap-2">
                          <Button type="button" variant="ghost" size="sm" onClick={() => setEditingId(null)}>
                            取消
                          </Button>
                          <Button type="submit" size="sm">
                            保存
                          </Button>
                        </div>
                      </form>
                    );
                  }
                  return (
                    <div
                      key={space.id}
                      onClick={() => handleSelectSpace(space.id)}
                      className={`flex cursor-pointer items-center justify-between rounded-lg border p-3 transition-all ${
                        isSelected
                          ? 'border-primary/50 bg-primary/5 shadow-xs'
                          : 'border-border bg-card hover:border-primary/30 hover:bg-muted/40'
                      }`}
                    >
                      <div className="flex items-center gap-3">
                        <div className="flex h-9 w-9 items-center justify-center rounded-md border border-border bg-background">
                          {getSpaceIcon(space.icon)}
                        </div>
                        <div>
                          <div className="flex items-center gap-2">
                            <span className="font-medium text-foreground text-sm">{space.name}</span>
                            {isSelected && <Check className="h-3.5 w-3.5 text-primary" />}
                          </div>
                          <p className="line-clamp-1 text-xs text-muted-foreground">{space.domain}</p>
                        </div>
                      </div>
                      <div className="flex items-center gap-2">
                        <ConfidenceRing value={space.expertise_overall || 0} max={100} size={30} strokeWidth={3} />
                        <button
                          type="button"
                          title="修改名称与领域"
                          onClick={(e) => {
                            e.stopPropagation();
                            startEdit(space);
                          }}
                          className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
                        >
                          <Pencil className="h-3.5 w-3.5" />
                        </button>
                      </div>
                    </div>
                  );
                })
              )}
            </div>
            <div className="mt-4 flex items-center justify-between border-t border-border pt-3">
              <button
                type="button"
                onClick={() => setIsCreating(true)}
                className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground"
              >
                <Plus className="h-3.5 w-3.5" />
                新建知识空间
              </button>
              <span className="text-[11px] text-muted-foreground">ESC 关闭</span>
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
