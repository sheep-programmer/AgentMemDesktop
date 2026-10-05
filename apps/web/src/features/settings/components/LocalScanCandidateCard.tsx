import React from 'react';
import type { LocalAgentCandidate } from '@/lib/api/types.temp';
import { Badge } from '@/components/ui/badge';
import {
  Bot,
  Terminal,
  Layers,
  KeyRound,
  Cpu,
  Laptop,
  Check,
  ShieldCheck,
  Key,
  AlertCircle,
  CheckCircle2,
} from 'lucide-react';

interface LocalScanCandidateCardProps {
  candidate: LocalAgentCandidate;
  isSelected: boolean;
  onToggle: (id: string) => void;
}

function getSourceIcon(source: LocalAgentCandidate['source']) {
  switch (source) {
    case 'claude_code':
      return <Bot className="h-4 w-4 text-amber-700 dark:text-amber-400 shrink-0" />;
    case 'codex':
      return <Terminal className="h-4 w-4 text-emerald-700 dark:text-emerald-400 shrink-0" />;
    case 'continue':
      return <Layers className="h-4 w-4 text-blue-600 dark:text-blue-400 shrink-0" />;
    case 'environment':
      return <KeyRound className="h-4 w-4 text-purple-600 dark:text-purple-400 shrink-0" />;
    case 'ollama':
      return <Cpu className="h-4 w-4 text-orange-700 dark:text-orange-400 shrink-0" />;
    case 'lm_studio':
      return <Laptop className="h-4 w-4 text-indigo-600 dark:text-indigo-400 shrink-0" />;
    default:
      return <Bot className="h-4 w-4 text-muted-foreground shrink-0" />;
  }
}

export function LocalScanCandidateCard({
  candidate,
  isSelected,
  onToggle,
}: LocalScanCandidateCardProps) {
  const isImported = candidate.already_imported;

  return (
    <div
      onClick={() => {
        if (!isImported) onToggle(candidate.suggested_id);
      }}
      className={`group relative flex items-start gap-3 rounded-xl border p-3.5 transition-all ${
        isImported
          ? 'border-border/40 bg-muted/20 opacity-60 cursor-not-allowed'
          : isSelected
            ? 'border-primary/60 bg-primary/5 shadow-xs cursor-pointer'
            : 'border-border/70 bg-card hover:border-border hover:bg-muted/30 cursor-pointer'
      }`}
    >
      {/* Checkbox */}
      <div className="pt-0.5 shrink-0">
        <div
          className={`h-4 w-4 rounded border flex items-center justify-center transition-colors ${
            isImported
              ? 'border-muted-foreground/30 bg-muted/40 cursor-not-allowed'
              : isSelected
                ? 'border-primary bg-primary text-primary-foreground shadow-xs'
                : 'border-muted-foreground/40 group-hover:border-foreground/60'
          }`}
        >
          {isImported ? (
            <Check className="h-3 w-3 text-muted-foreground" />
          ) : isSelected ? (
            <Check className="h-3 w-3" />
          ) : null}
        </div>
      </div>

      {/* Main Info */}
      <div className="flex-1 min-w-0 space-y-1.5">
        <div className="flex flex-wrap items-center gap-2 justify-between">
          <div className="flex items-center gap-1.5 min-w-0">
            {getSourceIcon(candidate.source)}
            <span className="font-semibold text-xs text-foreground truncate">
              {candidate.source_label}
            </span>
            {candidate.model && (
              <span className="text-xs font-mono font-medium text-foreground/90 bg-muted/60 px-1.5 py-0.5 rounded">
                {candidate.model}
              </span>
            )}
          </div>

          <div className="flex items-center gap-1.5 shrink-0">
            <span className="rounded bg-primary/10 px-1.5 py-0.5 font-mono text-[9px] font-semibold text-primary uppercase">
              {candidate.kind}
            </span>
            <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[9px] text-muted-foreground">
              {candidate.adapter}
            </span>
            {isImported && (
              <Badge variant="outline" className="bg-muted text-muted-foreground text-[10px] h-4 px-1.5">
                已导入
              </Badge>
            )}
          </div>
        </div>

        {/* Base URL */}
        {candidate.base_url && (
          <div className="font-mono text-[11px] text-muted-foreground truncate">
            {candidate.base_url}
          </div>
        )}

        {/* Key Source Tag */}
        <div className="flex flex-wrap items-center gap-1.5 pt-0.5">
          {candidate.api_key_env ? (
            <span className="inline-flex items-center gap-1 rounded-md bg-emerald-500/10 px-2 py-0.5 text-[11px] font-medium text-emerald-700 dark:text-emerald-400 border border-emerald-500/20">
              <ShieldCheck className="h-3 w-3 shrink-0" />
              将以 ${`{${candidate.api_key_env}}`} 占位符导入 · 明文不落盘
            </span>
          ) : candidate.api_key_hint ? (
            <span className="inline-flex items-center gap-1 rounded-md bg-muted px-2 py-0.5 text-[11px] font-medium text-muted-foreground border border-border">
              <Key className="h-3 w-3 shrink-0" />
              配置中为明文 {candidate.api_key_hint}
            </span>
          ) : candidate.source === 'ollama' || candidate.source === 'lm_studio' ? (
            <span className="inline-flex items-center gap-1 rounded-md bg-blue-500/10 px-2 py-0.5 text-[11px] font-medium text-blue-700 dark:text-blue-400 border border-blue-500/20">
              <CheckCircle2 className="h-3 w-3 shrink-0" />
              本地运行中 · 免密钥直连
            </span>
          ) : (
            <span className="inline-flex items-center gap-1 rounded-md bg-amber-500/10 px-2 py-0.5 text-[11px] font-medium text-amber-700 dark:text-amber-400 border border-amber-500/20">
              <AlertCircle className="h-3 w-3 shrink-0" />
              需导入后手动填写密钥
            </span>
          )}
        </div>

        {/* Note */}
        {candidate.note && (
          <p className="text-[11px] text-muted-foreground leading-snug">
            {candidate.note}
          </p>
        )}
      </div>
    </div>
  );
}
