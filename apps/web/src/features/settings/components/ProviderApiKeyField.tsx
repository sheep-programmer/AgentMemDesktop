import React from 'react';
import { ShieldCheck, Key } from 'lucide-react';

interface ProviderApiKeyFieldProps {
  mode: 'add' | 'edit';
  value: string;
  onChange: (val: string) => void;
  apiKeyFromEnv?: string | null;
  apiKeyHint?: string | null;
  isOllama?: boolean;
}

export function ProviderApiKeyField({
  mode,
  value,
  onChange,
  apiKeyFromEnv,
  apiKeyHint,
  isOllama,
}: ProviderApiKeyFieldProps) {
  return (
    <div>
      <label className="font-medium text-foreground">
        API Key {isOllama ? <span className="text-muted-foreground font-normal">(本地服务可留空)</span> : null}
      </label>
      {apiKeyFromEnv ? (
        <div className="mt-1 flex items-center gap-2 rounded-md border border-accent-insight/40 bg-accent-insight/5 px-3 py-1.5 text-xs text-accent-insight">
          <ShieldCheck className="h-4 w-4 shrink-0" />
          {/* api_key_from_env 本身就含 `${...}`，前面再写一个 $ 会显示成 $${VAR} */}
          <span>由环境变量 <code className="font-mono font-semibold">{apiKeyFromEnv}</code> 提供，只读不可修改</span>
        </div>
      ) : (
        <div className="relative mt-1">
          <input
            type="password"
            placeholder={
              mode === 'edit'
                ? (apiKeyHint ? `留空则保持不变 (当前: ${apiKeyHint})` : '留空则保持不变')
                : 'sk-•••••••••••• (本地模型可留空)'
            }
            value={value}
            onChange={(e) => onChange(e.target.value)}
            className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-xs outline-none font-mono pr-8"
          />
          <Key className="absolute right-2.5 top-2 h-3.5 w-3.5 text-muted-foreground pointer-events-none" />
        </div>
      )}
    </div>
  );
}
