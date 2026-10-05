import React from 'react';
import { cn } from '@/lib/utils';
import { formatShortcut } from '@/lib/platform';

interface KeyboardShortcutHintProps {
  shortcut: string;
  className?: string;
}

export function KeyboardShortcutHint({ shortcut, className }: KeyboardShortcutHintProps) {
  return (
    <kbd
      className={cn(
        'pointer-events-none inline-flex h-5 select-none items-center gap-0.5 rounded border border-border bg-muted px-1.5 font-mono text-[10px] font-medium text-muted-foreground opacity-90',
        className,
      )}
    >
      {formatShortcut(shortcut)}
    </kbd>
  );
}
