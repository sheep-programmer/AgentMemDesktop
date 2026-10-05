import React from 'react';
import { COMMON_DIMENSIONS } from './providerPresets';

interface ProviderDimensionFieldProps {
  dimension?: number;
  onChange: (dim?: number) => void;
}

export function ProviderDimensionField({ dimension, onChange }: ProviderDimensionFieldProps) {
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between">
        <label className="font-medium text-foreground">向量维度 (Dimension)</label>
        <div className="flex gap-1">
          {COMMON_DIMENSIONS.map((dim) => (
            <button
              key={dim}
              type="button"
              onClick={() => onChange(dim)}
              className={`rounded px-1.5 py-0.2 text-[10px] border transition-colors ${
                dimension === dim
                  ? 'border-primary bg-primary/10 text-primary font-medium'
                  : 'border-border text-muted-foreground hover:bg-muted'
              }`}
            >
              {dim}
            </button>
          ))}
        </div>
      </div>
      <input
        type="number"
        placeholder="例如: 1536, 1024, 768"
        value={dimension || ''}
        onChange={(e) => onChange(Number(e.target.value) || undefined)}
        className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-xs outline-none font-mono"
      />
    </div>
  );
}
