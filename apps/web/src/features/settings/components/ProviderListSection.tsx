import React, { useState } from 'react';
import type { ProviderItem } from '@/lib/api/types.temp';
import { providerService } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { AddProviderDialog } from './AddProviderDialog';
import { EditProviderDialog } from './EditProviderDialog';
import { LocalScanDialog } from './LocalScanDialog';
import { ProviderEmptyState } from './ProviderEmptyState';
import { ProviderListItem } from './ProviderListItem';
import { Plus, Laptop } from 'lucide-react';
import { toast } from 'sonner';

interface ProviderListSectionProps {
  providers: ProviderItem[];
  onAddProvider: (provider: ProviderItem) => void;
  onUpdateProvider: (provider: ProviderItem) => void;
  onImportLocal: (providers: ProviderItem[]) => void;
  onDeleteProvider: (id: string) => void;
}

export function ProviderListSection({
  providers,
  onAddProvider,
  onUpdateProvider,
  onImportLocal,
  onDeleteProvider,
}: ProviderListSectionProps) {
  const [isAddOpen, setIsAddOpen] = useState(false);
  const [isScanOpen, setIsScanOpen] = useState(false);
  const [editingProvider, setEditingProvider] = useState<ProviderItem | null>(null);
  const [isEditOpen, setIsEditOpen] = useState(false);
  const [testingId, setTestingId] = useState<string | null>(null);

  const testConnection = async (prov: ProviderItem) => {
    setTestingId(prov.id);
    const toastId = toast.loading(`正在探测 ${prov.id} 连通性...`);
    try {
      const health = await providerService.checkHealth(prov.id);
      if (health.ok) {
        toast.success(`连接成功！往返延迟: ${health.latency_ms}ms`, { id: toastId });
        onUpdateProvider({
          ...prov,
          status: 'online',
          latency_ms: health.latency_ms ?? 0,
        });
      } else {
        toast.error(`连通性异常: ${health.error || '无法建立连接'}`, { id: toastId });
        onUpdateProvider({
          ...prov,
          status: 'offline',
        });
      }
    } catch (err: unknown) {
      toast.error(`测试失败: ${(err as Error)?.message || '网络连接超时'}`, { id: toastId });
      onUpdateProvider({
        ...prov,
        status: 'offline',
      });
    } finally {
      setTestingId(null);
    }
  };

  const handleOpenEdit = (prov: ProviderItem) => {
    setEditingProvider(prov);
    setIsEditOpen(true);
  };

  return (
    <div className="rounded-2xl border border-border bg-card p-6 space-y-4">
      <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-foreground">模型服务商</h3>
          <p className="text-xs text-muted-foreground mt-0.5">
            配置接入云端 API (DeepSeek / Anthropic / OpenAI) 或本地部署的推理引擎。
          </p>
        </div>

        <div className="flex items-center gap-2">
          <Button
            size="sm"
            variant="outline"
            onClick={() => setIsScanOpen(true)}
            className="gap-1.5 text-xs"
          >
            <Laptop className="h-3.5 w-3.5 text-primary" />
            扫描本机
          </Button>
          <Button
            size="sm"
            onClick={() => setIsAddOpen(true)}
            className="gap-1.5 text-xs"
          >
            <Plus className="h-3.5 w-3.5" />
            添加服务商
          </Button>
        </div>
      </div>

      {providers.length === 0 ? (
        <ProviderEmptyState
          onAddClick={() => setIsAddOpen(true)}
          onScanClick={() => setIsScanOpen(true)}
        />
      ) : (
        <div className="divide-y divide-border/50 border border-border/70 rounded-xl overflow-hidden bg-background/50">
          {providers.map((prov) => (
            <ProviderListItem
              key={prov.id}
              prov={prov}
              isTesting={testingId === prov.id}
              onTest={testConnection}
              onEdit={handleOpenEdit}
              onDelete={onDeleteProvider}
            />
          ))}
        </div>
      )}

      <AddProviderDialog
        isOpen={isAddOpen}
        onClose={() => setIsAddOpen(false)}
        onAdd={onAddProvider}
      />

      <EditProviderDialog
        isOpen={isEditOpen}
        onClose={() => {
          setIsEditOpen(false);
          setEditingProvider(null);
        }}
        provider={editingProvider}
        onUpdate={onUpdateProvider}
      />

      <LocalScanDialog
        isOpen={isScanOpen}
        onClose={() => setIsScanOpen(false)}
        onImport={onImportLocal}
        existingProviders={providers}
      />
    </div>
  );
}
