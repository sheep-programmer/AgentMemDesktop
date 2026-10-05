import React from 'react';

interface EvidenceEmptyStateProps {
  icon: React.ComponentType<{ className?: string }>;
  title: string;
  desc: string;
}

export function EvidenceEmptyState({ icon: Icon, title, desc }: EvidenceEmptyStateProps) {
  return (
    <div className="flex h-[calc(100vh-140px)] flex-col items-center justify-center px-6 py-12 text-center">
      <div className="flex h-10 w-10 items-center justify-center rounded-xl border border-border/70 bg-muted/40 text-muted-foreground mb-3 shadow-2xs">
        <Icon className="h-5 w-5" />
      </div>
      <div className="text-xs font-semibold text-foreground/90">{title}</div>
      <p className="mt-1.5 text-[11px] leading-relaxed text-muted-foreground max-w-[220px]">
        {desc}
      </p>
    </div>
  );
}
