import React, { useState, useEffect } from 'react';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import type { ProviderItem, ProviderUpdate } from '@/lib/api/types.temp';
import { providerService } from '@/lib/api';
import { toast } from 'sonner';
import { ProviderFormFields, type ProviderFormData } from './ProviderFormFields';
import { isLocalAdapter } from './providerPresets';

interface ProviderDialogProps {
  isOpen: boolean;
  onClose: () => void;
  mode: 'add' | 'edit';
  initialData?: ProviderItem | null;
  onSubmitSuccess: (provider: ProviderItem) => void;
}

const DEFAULT_FORM_DATA: ProviderFormData = {
  providerId: '',
  kind: 'llm',
  adapter: 'openai_compatible',
  baseUrl: 'https://api.openai.com/v1',
  apiKey: '',
  model: 'gpt-4o',
  dimension: undefined,
  device: 'auto',
};

export function ProviderDialog({
  isOpen,
  onClose,
  mode,
  initialData,
  onSubmitSuccess,
}: ProviderDialogProps) {
  const [formData, setFormData] = useState<ProviderFormData>(DEFAULT_FORM_DATA);
  const [isSubmitting, setIsSubmitting] = useState(false);

  useEffect(() => {
    if (!isOpen) return;
    if (mode === 'edit' && initialData) {
      setFormData({
        providerId: initialData.id,
        kind: initialData.kind,
        adapter: initialData.adapter,
        baseUrl: initialData.base_url || '',
        apiKey: '',
        model: initialData.model || '',
        dimension: initialData.dimension ?? undefined,
        device: initialData.device || 'auto',
      });
    } else {
      setFormData(DEFAULT_FORM_DATA);
    }
  }, [isOpen, mode, initialData]);

  const handleChange = (patch: Partial<ProviderFormData>) => {
    setFormData((prev) => ({ ...prev, ...patch }));
  };

  const handleCreate = async (pid: string) => {
    const isLocal = isLocalAdapter(formData.adapter);
    const created = await providerService.createProvider({
      id: pid,
      kind: formData.kind,
      adapter: formData.adapter,
      base_url: isLocal ? null : (formData.baseUrl.trim() || null),
      api_key: isLocal ? null : (formData.apiKey.trim() || null),
      model: formData.model.trim(),
      dimension: formData.kind === 'embedding' ? (formData.dimension || 1024) : null,
      device: isLocal ? formData.device : null,
      enabled: true,
    });
    return { ...created, name: created.id, status: 'online' as const, latency_ms: 45 };
  };

  const handleUpdate = async () => {
    if (!initialData) throw new Error('缺少服务商原始数据');
    const isLocal = isLocalAdapter(formData.adapter);
    const payload: ProviderUpdate = {
      kind: formData.kind,
      adapter: formData.adapter,
      model: formData.model.trim(),
      base_url: isLocal ? null : (formData.baseUrl.trim() || null),
      dimension: formData.kind === 'embedding' ? (formData.dimension || 1024) : null,
      device: isLocal ? formData.device : null,
    };
    if (!isLocal && !initialData.api_key_from_env && formData.apiKey.trim()) {
      payload.api_key = formData.apiKey.trim();
    }
    const updated = await providerService.updateProvider(initialData.id, payload);
    return {
      ...initialData,
      ...updated,
      name: updated.id,
      status: initialData.status || 'online',
      latency_ms: initialData.latency_ms,
    };
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!formData.model.trim()) {
      toast.error('请填写模型标识 (Model)');
      return;
    }

    setIsSubmitting(true);
    try {
      if (mode === 'add') {
        const pid = (formData.providerId.trim() || `${formData.adapter}-${formData.model}`)
          .toLowerCase()
          .replace(/[^a-z0-9-_]/g, '-');
        const item = await handleCreate(pid);
        onSubmitSuccess(item);
        toast.success(`已成功添加模型服务商: ${item.id}`);
      } else {
        const item = await handleUpdate();
        onSubmitSuccess(item);
        toast.success(`已保存服务商设置: ${item.id}`);
      }
      onClose();
    } catch (err: unknown) {
      toast.error(`操作失败: ${(err as Error)?.message || '未知错误'}`);
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <Dialog open={isOpen} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle className="text-base font-semibold">
            {mode === 'add' ? '添加模型服务商' : `编辑服务商: ${initialData?.id || ''}`}
          </DialogTitle>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="space-y-4">
          <ProviderFormFields
            mode={mode}
            formData={formData}
            onChange={handleChange}
            apiKeyFromEnv={initialData?.api_key_from_env}
            apiKeyHint={initialData?.api_key_hint}
          />

          <div className="flex justify-end gap-2 pt-3 border-t border-border/50">
            <Button type="button" variant="ghost" size="sm" onClick={onClose} disabled={isSubmitting}>
              取消
            </Button>
            <Button type="submit" size="sm" disabled={isSubmitting}>
              {isSubmitting ? '正在保存...' : mode === 'add' ? '确认添加' : '保存修改'}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
