import React from 'react';
import type { ProviderKind, ProviderAdapter } from '@/lib/api/types.temp';
import {
  ADAPTERS_BY_KIND,
  QUICK_PRESETS,
  DEVICE_OPTIONS,
  isLocalAdapter,
  getDefaultAdapter,
} from './providerPresets';
import { ProviderApiKeyField } from './ProviderApiKeyField';
import { ProviderDimensionField } from './ProviderDimensionField';
import { Cpu } from 'lucide-react';

export interface ProviderFormData {
  providerId: string;
  kind: ProviderKind;
  adapter: ProviderAdapter;
  baseUrl: string;
  apiKey: string;
  model: string;
  dimension?: number;
  device: string;
}

interface ProviderFormFieldsProps {
  mode: 'add' | 'edit';
  formData: ProviderFormData;
  onChange: (data: Partial<ProviderFormData>) => void;
  apiKeyFromEnv?: string | null;
  apiKeyHint?: string | null;
}

export function ProviderFormFields({
  mode,
  formData,
  onChange,
  apiKeyFromEnv,
  apiKeyHint,
}: ProviderFormFieldsProps) {
  const isLocal = isLocalAdapter(formData.adapter);
  const isOllama = formData.adapter === 'ollama_native' || formData.baseUrl.includes(':11434');

  const handleKindChange = (nextKind: ProviderKind) => {
    const validAdapters = ADAPTERS_BY_KIND[nextKind].map((a) => a.value);
    const nextAdapter = validAdapters.includes(formData.adapter)
      ? formData.adapter
      : getDefaultAdapter(nextKind);
    onChange({
      kind: nextKind,
      adapter: nextAdapter,
      dimension: nextKind === 'embedding' ? (formData.dimension || 1024) : undefined,
    });
  };

  const handleAdapterChange = (nextAdapter: ProviderAdapter) => {
    let nextBaseUrl = formData.baseUrl;
    if (nextAdapter === 'anthropic' && (!formData.baseUrl || formData.baseUrl.includes('openai'))) {
      nextBaseUrl = 'https://api.anthropic.com';
    } else if (nextAdapter === 'ollama_native' && !formData.baseUrl) {
      nextBaseUrl = 'http://localhost:11434';
    }
    onChange({ adapter: nextAdapter, baseUrl: nextBaseUrl });
  };

  return (
    <div className="space-y-3 pt-1 text-xs">
      <div>
        <label className="font-medium text-foreground">
          服务商标识 (ID) {mode === 'edit' && <span className="text-[10px] text-muted-foreground font-normal">(只读，不可修改)</span>}
        </label>
        <input
          type="text"
          disabled={mode === 'edit'}
          placeholder="例如: deepseek-chat (留空自动生成)"
          value={formData.providerId}
          onChange={(e) => onChange({ providerId: e.target.value })}
          className="mt-1 w-full rounded-md border border-input bg-background px-3 py-1.5 text-xs outline-none disabled:opacity-60 disabled:bg-muted/40 font-mono"
        />
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="font-medium text-foreground">类别 (Kind)</label>
          <select
            value={formData.kind}
            onChange={(e) => handleKindChange(e.target.value as ProviderKind)}
            className="mt-1 w-full rounded-md border border-input bg-background px-2.5 py-1.5 text-xs outline-none"
          >
            <option value="llm">语言模型 (LLM)</option>
            <option value="embedding">向量嵌入 (Embedding)</option>
            <option value="rerank">精排重排 (Rerank)</option>
          </select>
        </div>

        <div>
          <label className="font-medium text-foreground">适配协议 (Adapter)</label>
          <select
            value={formData.adapter}
            onChange={(e) => handleAdapterChange(e.target.value as ProviderAdapter)}
            className="mt-1 w-full rounded-md border border-input bg-background px-2.5 py-1.5 text-xs outline-none"
          >
            {ADAPTERS_BY_KIND[formData.kind].map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div>
        <label className="font-medium text-foreground">模型标识 (Model)</label>
        <input
          type="text"
          required
          placeholder="例如: deepseek-chat, gpt-4o, BAAI/bge-m3"
          value={formData.model}
          onChange={(e) => onChange({ model: e.target.value })}
          className="mt-1 w-full rounded-md border border-input bg-background px-3 py-1.5 text-xs outline-none font-mono"
        />
      </div>

      {formData.adapter === 'openai_compatible' && mode === 'add' && (
        <div className="space-y-1">
          <span className="text-[10px] text-muted-foreground">快速预设填入:</span>
          <div className="flex flex-wrap gap-1.5">
            {QUICK_PRESETS.map((p) => (
              <button
                key={p.label}
                type="button"
                onClick={() => onChange({ baseUrl: p.baseUrl, model: p.model })}
                className="rounded border border-border/80 bg-muted/30 px-2 py-0.5 text-[11px] text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
              >
                {p.label}
              </button>
            ))}
          </div>
        </div>
      )}

      {isLocal ? (
        <div className="rounded-lg border border-border/60 bg-muted/20 p-2.5 space-y-2">
          <div className="flex items-center gap-1.5 text-[11px] font-medium text-foreground">
            <Cpu className="h-3.5 w-3.5 text-primary" />
            <span>本地引擎配置 (免密 & 无需 Base URL)</span>
          </div>
          <div>
            <label className="text-[11px] text-muted-foreground">运算加速设备 (Device)</label>
            <select
              value={formData.device}
              onChange={(e) => onChange({ device: e.target.value })}
              className="mt-1 w-full rounded-md border border-input bg-background px-2.5 py-1.5 text-xs outline-none font-mono"
            >
              {DEVICE_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
              ))}
            </select>
          </div>
        </div>
      ) : (
        <>
          <div>
            <div className="flex items-center justify-between">
              <label className="font-medium text-foreground">API Base URL</label>
              {isOllama && (
                <span className="text-[10px] text-muted-foreground">本地 Ollama 默认: http://localhost:11434</span>
              )}
            </div>
            <input
              type="text"
              placeholder="例如: https://api.deepseek.com/v1"
              value={formData.baseUrl}
              onChange={(e) => onChange({ baseUrl: e.target.value })}
              className="mt-1 w-full rounded-md border border-input bg-background px-3 py-1.5 text-xs outline-none font-mono"
            />
          </div>

          <ProviderApiKeyField
            mode={mode}
            value={formData.apiKey}
            onChange={(val) => onChange({ apiKey: val })}
            apiKeyFromEnv={apiKeyFromEnv}
            apiKeyHint={apiKeyHint}
            isOllama={isOllama}
          />
        </>
      )}

      {formData.kind === 'embedding' && (
        <ProviderDimensionField
          dimension={formData.dimension}
          onChange={(dim) => onChange({ dimension: dim })}
        />
      )}
    </div>
  );
}
