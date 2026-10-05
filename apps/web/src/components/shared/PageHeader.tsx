import type { ReactNode } from 'react';
import type { LucideIcon } from 'lucide-react';

interface PageHeaderProps {
  title: string;
  description: string;
  eyebrow: string;
  icon: LucideIcon;
  children?: ReactNode;
}

export function PageHeader({
  title,
  description,
  eyebrow,
  icon: Icon,
  children,
}: PageHeaderProps) {
  return (
    <div className="workspace-heading">
      <div className="min-w-0 max-w-2xl">
        <div className="mb-2.5 flex items-center gap-1.5 text-[11.5px] font-medium tracking-wide text-muted-foreground">
          <Icon className="h-3.5 w-3.5 text-primary" aria-hidden="true" />
          <span>{eyebrow}</span>
        </div>
        <h1 className="font-display text-[26px] leading-tight text-foreground sm:text-[30px]">
          {title}
        </h1>
        <p className="mt-2 max-w-xl text-[13.5px] leading-relaxed text-muted-foreground">
          {description}
        </p>
      </div>
      {children && (
        <div className="flex min-w-0 max-w-full flex-wrap items-center gap-2">
          {children}
        </div>
      )}
    </div>
  );
}
