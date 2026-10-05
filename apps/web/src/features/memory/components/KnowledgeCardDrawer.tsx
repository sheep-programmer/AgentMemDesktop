import React, { useState, useEffect } from 'react';
import type { KnowledgeCard } from '@/lib/api/types.temp';
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Button } from '@/components/ui/button';
import { MarkdownView } from '@/components/shared/MarkdownView';
import { ConfidenceRing } from '@/components/shared/ConfidenceRing';
import { BadgeCheck, Save, Eye, FileText, History, Trash2 } from 'lucide-react';
import { CardVersionHistory } from './CardVersionHistory';
import { useConfirm } from '@/components/shared/ConfirmProvider';

interface KnowledgeCardDrawerProps {
  card: KnowledgeCard | null;
  spaceId?: string;
  isOpen: boolean;
  initialTab?: 'edit' | 'history';
  onClose: () => void;
  onSave: (updated: KnowledgeCard) => void;
  /** 删除这张卡片。抽错、重复或有幻觉的卡片此前在界面上删不掉 */
  onDelete?: (card: KnowledgeCard) => void;
}

export function KnowledgeCardDrawer({
  card,
  spaceId,
  isOpen,
  initialTab = 'edit',
  onClose,
  onSave,
  onDelete,
}: KnowledgeCardDrawerProps) {
  const requestConfirmation = useConfirm();
  const [activeTab, setActiveTab] = useState<'edit' | 'preview' | 'history'>('edit');
  const [title, setTitle] = useState('');
  const [body, setBody] = useState('');

  useEffect(() => {
    if (isOpen) {
      setActiveTab(initialTab);
    }
  }, [isOpen, initialTab]);

  useEffect(() => {
    if (card) {
      setTitle(card.title);
      setBody(card.body);
    }
  }, [card]);

  if (!card) return null;

  const handleSave = () => {
    const updated: KnowledgeCard = {
      ...card,
      title,
      body,
      verified_by: 'user',
      updated_at: Date.now(),
    };
    // 成功提示与关闭由调用方在保存成功后做：此前这里先报「已保存」再关抽屉，
    // 保存失败时用户看到的是一句成功加一句失败
    onSave(updated);
  };

  return (
    <Sheet open={isOpen} onOpenChange={(open) => !open && onClose()}>
      <SheetContent className="w-full sm:max-w-2xl md:max-w-3xl p-6 flex flex-col justify-between overflow-y-auto">
        <div className="space-y-4">
          <SheetHeader>
            <div className="flex items-center justify-between">
              <span className="font-mono text-xs uppercase text-muted-foreground">
                [{card.kind}] 知识卡片
              </span>
              <div className="flex items-center gap-2">
                {card.verified_by && (
                  <span className="inline-flex items-center gap-1 rounded-md bg-accent-insight/10 px-2 py-0.5 text-[10px] font-medium text-accent-insight border border-accent-insight/25">
                    <BadgeCheck className="h-3 w-3" />
                    已人工校验
                  </span>
                )}
                <ConfidenceRing value={card.confidence} size={28} strokeWidth={3} />
              </div>
            </div>
            <SheetTitle className="text-base font-semibold mt-1">知识卡片详情</SheetTitle>

            {/* 标签页切换：卡片编辑 vs 版本历史 */}
            <Tabs
              value={activeTab}
              onValueChange={(v) => setActiveTab(v as 'edit' | 'preview' | 'history')}
              className="w-full pt-1"
            >
              <TabsList className="grid w-full grid-cols-3 bg-muted/60 p-1">
                <TabsTrigger value="edit" className="gap-1.5 text-xs">
                  <FileText className="h-3.5 w-3.5" />
                  卡片内容与编辑
                </TabsTrigger>
                <TabsTrigger value="preview" className="gap-1.5 text-xs">
                  <Eye className="h-3.5 w-3.5" />
                  渲染预览
                </TabsTrigger>
                <TabsTrigger value="history" className="gap-1.5 text-xs">
                  <History className="h-3.5 w-3.5" />
                  版本留档历史
                </TabsTrigger>
              </TabsList>
            </Tabs>
          </SheetHeader>

          {activeTab === 'edit' && (
            <div className="space-y-3 pt-2">
              <div>
                <label className="text-xs font-medium text-foreground">知识卡片标题</label>
                <input
                  type="text"
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  className="mt-1 w-full rounded-md border border-input bg-background px-3 py-1.5 text-xs text-foreground outline-none focus:border-primary focus:ring-1 focus:ring-primary/30"
                />
              </div>

              <div>
                <label className="text-xs font-medium text-foreground">卡片正文</label>
                <textarea
                  rows={11}
                  value={body}
                  onChange={(e) => setBody(e.target.value)}
                  className="mt-1 w-full rounded-md border border-input bg-background p-3 text-xs text-foreground outline-none resize-none font-mono focus:border-primary focus:ring-1 focus:ring-primary/30 leading-relaxed"
                />
              </div>

              {card.aliases && card.aliases.length > 0 && (
                <div>
                  <label className="text-xs font-medium text-foreground">别名关联</label>
                  <div className="mt-1.5 flex flex-wrap gap-1.5">
                    {card.aliases.map((alias) => (
                      <span
                        key={alias}
                        className="rounded-md border border-border/70 bg-muted/40 px-2 py-0.5 font-mono text-[11px] text-muted-foreground"
                      >
                        #{alias}
                      </span>
                    ))}
                  </div>
                </div>
              )}

              <div className="rounded-lg border border-border/60 bg-muted/20 p-3 text-xs text-muted-foreground flex items-center gap-2">
                <BadgeCheck className="h-4 w-4 text-accent-insight shrink-0" />
                <span>人工编辑保存后将自动标记为「已人工校验」，在混合检索与模型生成时享有最高优先级。</span>
              </div>
            </div>
          )}

          {activeTab === 'preview' && (
            <div className="space-y-2 pt-2">
              <div className="flex items-center justify-between text-xs text-muted-foreground">
                <span>正文按 Markdown 渲染，表格、列表与代码块都会展开</span>
                <span className="font-mono text-[10px]">
                  {body !== card.body ? '预览的是未保存的草稿' : '当前版本'}
                </span>
              </div>
              <div className="rounded-lg border border-border/70 bg-card/40 p-4 prose prose-sm dark:prose-invert max-w-none">
                <MarkdownView>{body || '*(正文为空)*'}</MarkdownView>
              </div>
            </div>
          )}

          {activeTab === 'history' && (
            <div className="pt-2">
              <CardVersionHistory
                card={card}
                spaceId={spaceId || card.space_id}
              />
            </div>
          )}
        </div>

        {activeTab === 'edit' ? (
          <div className="flex items-center justify-end gap-2 border-t border-border pt-4 mt-6">
            {onDelete && (
              <Button
                variant="ghost"
                size="sm"
                className="mr-auto gap-1.5 text-destructive hover:bg-destructive/10 hover:text-destructive"
                onClick={async () => {
                  const accepted = await requestConfirmation({
                    title: `删除卡片「${card.title}」？`,
                    description: '它的历史版本会一起删除，检索也不会再用到它。',
                    confirmText: '删除卡片',
                    destructive: true,
                  });
                  if (accepted) onDelete(card);
                }}
              >
                <Trash2 className="h-3.5 w-3.5" />
                删除卡片
              </Button>
            )}
            <Button variant="outline" size="sm" onClick={onClose}>
              取消
            </Button>
            <Button size="sm" onClick={handleSave} className="gap-1.5">
              <Save className="h-3.5 w-3.5" />
              保存校验结果
            </Button>
          </div>
        ) : (
          <div className="flex items-center justify-between border-t border-border pt-4 mt-6 text-xs text-muted-foreground">
            <span className="text-[11px]">正文或标题被改动时将自动沉淀新版本快照</span>
            <div className="flex items-center gap-2">
              <Button variant="outline" size="sm" onClick={() => setActiveTab('edit')}>
                编辑当前卡片
              </Button>
              <Button size="sm" onClick={onClose}>
                完成
              </Button>
            </div>
          </div>
        )}
      </SheetContent>
    </Sheet>
  );
}
