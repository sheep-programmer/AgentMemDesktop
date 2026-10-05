import { PageHeader } from '@/components/shared/PageHeader';
import React, { useState, useEffect } from 'react';
import type {
  ModelRole,
  RoleBindings,
  ProviderItem,
} from '@/lib/api/types.temp';
import { providerService } from '@/lib/api';
import { spaceService } from '@/lib/api/services/spaces';
import { useSpaceStore } from '@/stores/useSpaceStore';
import { RoleBindingsSection } from './components/RoleBindingsSection';
import { ProviderListSection } from './components/ProviderListSection';
import { ContextCostSection } from './components/ContextCostSection';
import { AppearanceSection } from './components/AppearanceSection';
import { DataStorageSection } from './components/DataStorageSection';
import { AboutSection } from './components/AboutSection';
import { FourStateView } from '@/components/shared/FourStateView';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Cpu, Palette, HardDrive, Info } from 'lucide-react';
import { toast } from 'sonner';
import { ROLE_NAMES } from './lib/singleModelPlan';
import { useConfirm } from '@/components/shared/ConfirmProvider';

export function SettingsPage() {
  const requestConfirmation = useConfirm();
  const [providers, setProviders] = useState<ProviderItem[]>([]);
  const [bindings, setBindings] = useState<RoleBindings>({});
  const [activeTab, setActiveTab] = useState<
    'models' | 'appearance' | 'data' | 'about'
  >('models');
  const [pageStatus, setPageStatus] = useState<
    'loading' | 'empty' | 'error' | 'ready'
  >('loading');

  const loadData = async () => {
    try {
      setPageStatus('loading');
      const [provList, roles] = await Promise.all([
        providerService.getProviders(),
        providerService.getRoleBindings(),
      ]);
      setProviders(provList);
      setBindings(roles);
      setPageStatus('ready');
    } catch (err: unknown) {
      console.error('Failed to load settings data:', err);
      setPageStatus('error');
    }
  };

  useEffect(() => {
    loadData();
  }, []);

  /**
   * 整体提交一份角色绑定（接口是整体覆盖）。界面先乐观更新，失败时退回提交前的绑定，
   * 免得下拉框显示着一个其实没存上的服务商。返回是否成功，供确认框决定要不要关闭。
   */
  const handleUpdateBindings = async (
    updatedBindings: RoleBindings,
    successMessage = '角色绑定已更新',
  ): Promise<boolean> => {
    const previous = bindings;
    setBindings(updatedBindings);
    try {
      const res = await providerService.updateRoleBindings(updatedBindings);
      setBindings(res.roles);
      if (res.requires_reindex) {
        await reindexAllSpaces();
      } else {
        toast.success(successMessage);
      }
      return true;
    } catch (err: unknown) {
      setBindings(previous);
      toast.error(
        `更新角色绑定失败: ${(err as Error)?.message || '网络错误'}`,
        {
          description: '已恢复为原来的绑定。',
        },
      );
      return false;
    }
  };

  const handleUpdateBinding = (role: ModelRole, providerId: string) =>
    handleUpdateBindings({ ...bindings, [role]: providerId });

  /**
   * 换了向量模型之后逐个空间重建索引。
   *
   * 此前弹窗按钮写着「确认变更并触发全量重建索引」，实际只改了绑定：维度不同时之后
   * 每篇新文档的向量化都失败，维度相同则旧向量与新模型语义不兼容、静默召回错误，
   * 而界面上没有任何能手动重建的入口。
   */
  const reindexAllSpaces = async () => {
    const spaces = useSpaceStore.getState().spaces;
    const toastId = toast.loading('向量模型已更换，正在重建索引…', {
      duration: Infinity,
    });
    let failures = 0;
    for (const [index, space] of spaces.entries()) {
      const label = `（${index + 1}/${spaces.length}）「${space.name}」`;
      try {
        await spaceService.reindexSpace(space.id, {
          onProgress: ({ done, total }) =>
            toast.loading(`正在重建索引${label}：${done}/${total} 篇`, {
              id: toastId,
            }),
          onError: () => {
            failures += 1;
          },
        });
      } catch {
        failures += 1;
      }
    }
    if (failures > 0) {
      toast.error(`索引重建完成，但有 ${failures} 处失败`, {
        id: toastId,
        description: '失败的文档可以在资料库里点「重新解析」再试。',
        duration: 10000,
      });
    } else {
      toast.success(`已为 ${spaces.length} 个知识空间重建索引`, {
        id: toastId,
        duration: 5000,
      });
    }
  };

  const handleUpdateProvider = (updated: ProviderItem) => {
    setProviders((prev) =>
      prev.map((p) => (p.id === updated.id ? updated : p)),
    );
  };

  return (
    <div className="workspace-page">
      <div className="workspace-content space-y-6">
        <FourStateView
          status={pageStatus}
          emptyTitle="系统配置为空"
          emptyDescription="暂未检测到模型服务商，请点击右上角添加。"
          error="无法加载当前系统的配置项，请检查后端运行状态。"
          onRetry={loadData}
        >
          <PageHeader
            title="打造适合你的知识工作环境"
            eyebrow="偏好设置 / 系统"
            icon={Cpu}
            description="配置模型能力、选择外观主题，掌握本地数据与存储状态。"
          >
            <Tabs
              value={activeTab}
              onValueChange={(v) =>
                setActiveTab(v as 'models' | 'appearance' | 'data' | 'about')
              }
            >
              <TabsList className="bg-muted/50 p-1">
                <TabsTrigger value="models" className="gap-1.5 text-xs">
                  <Cpu className="h-3.5 w-3.5" />
                  模型绑定
                </TabsTrigger>
                <TabsTrigger value="appearance" className="gap-1.5 text-xs">
                  <Palette className="h-3.5 w-3.5" />
                  外观与主题
                </TabsTrigger>
                <TabsTrigger value="data" className="gap-1.5 text-xs">
                  <HardDrive className="h-3.5 w-3.5" />
                  数据与存储
                </TabsTrigger>
                <TabsTrigger value="about" className="gap-1.5 text-xs">
                  <Info className="h-3.5 w-3.5" />
                  关于系统
                </TabsTrigger>
              </TabsList>
            </Tabs>
          </PageHeader>

          {activeTab === 'models' && (
            <div className="space-y-6">
              <RoleBindingsSection
                bindings={bindings}
                providers={providers}
                onUpdateBinding={handleUpdateBinding}
                onUpdateBindings={handleUpdateBindings}
              />
              <ProviderListSection
                providers={providers}
                onAddProvider={(p) => setProviders((prev) => [p, ...prev])}
                onUpdateProvider={handleUpdateProvider}
                onImportLocal={(items) =>
                  setProviders((prev) => [...items, ...prev])
                }
                onDeleteProvider={async (id) => {
                  // 删掉就得重新填地址、密钥和模型名，不能一点就没
                  const boundRoles = Object.entries(bindings)
                    .filter(([, providerId]) => providerId === id)
                    .map(([role]) => ROLE_NAMES[role as ModelRole] ?? role);
                  if (boundRoles.length > 0) {
                    toast.error(`「${id}」还在被使用，不能删除`, {
                      description: `正用于：${boundRoles.join('、')}。先在上面把这些角色改绑到别的服务商。`,
                    });
                    return;
                  }
                  const accepted = await requestConfirmation({
                    title: `删除服务商「${id}」？`,
                    description: '删除后它的地址、模型名和密钥配置都要重新填写。',
                    confirmText: '删除服务商',
                    destructive: true,
                  });
                  if (!accepted) {
                    return;
                  }
                  try {
                    await providerService.deleteProvider(id);
                    setProviders((prev) => prev.filter((p) => p.id !== id));
                    toast.success('已删除服务商');
                  } catch (err: unknown) {
                    toast.error(
                      `删除失败: ${(err as Error)?.message || '无法删除'}`,
                    );
                  }
                }}
              />
              <ContextCostSection />
            </div>
          )}

          {activeTab === 'appearance' && <AppearanceSection />}
          {activeTab === 'data' && <DataStorageSection />}
          {activeTab === 'about' && <AboutSection />}
        </FourStateView>
      </div>
    </div>
  );
}
