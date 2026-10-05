import type { ReactNode } from 'react';
import type { LucideIcon } from 'lucide-react';
import { ChevronDown } from 'lucide-react';

import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
} from '@/components/ui/dropdown-menu';
import { cn } from '@/lib/utils';

export interface OptionCard<T extends string> {
  value: T;
  title: string;
  description?: string;
  icon: LucideIcon;
  /** 标题右侧的小徽章，如「本地」「推荐」。 */
  badge?: ReactNode;
  disabled?: boolean;
}

interface OptionCardMenuProps<T extends string> {
  /** 触发按钮的无障碍名称；对比度检查脚本也按它找按钮。 */
  ariaLabel: string;
  /** 触发按钮里的图标与文字。 */
  icon: LucideIcon;
  label: ReactNode;
  /** 菜单顶部的小标题。 */
  heading?: string;
  value: T;
  options: OptionCard<T>[];
  onChange: (value: T) => void;
  disabled?: boolean;
  /** 菜单底部的附加内容，如「管理模型」入口。 */
  footer?: ReactNode;
  triggerClassName?: string;
  align?: 'start' | 'center' | 'end';
}

/**
 * 输入框底部那排选项的统一菜单：每个选项是一张卡片（图标 + 名称 + 一句用途说明），
 * 当前选中的那张描边高亮并带勾。
 *
 * 此前三个菜单三种长相：上下文是卡片，检索是一排裸文字，模型干脆不是菜单。
 * 纯文字选项看不出「混合增强」和「仅向量」差在哪，用户只能盲选。
 */
export function OptionCardMenu<T extends string>({
  ariaLabel,
  icon: Icon,
  label,
  heading,
  value,
  options,
  onChange,
  disabled,
  footer,
  triggerClassName,
  align = 'start',
}: OptionCardMenuProps<T>) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        aria-label={ariaLabel}
        disabled={disabled}
        className={cn(
          'composer-chip flex h-7 items-center gap-1.5 rounded-full px-2.5 text-[12px] text-muted-foreground outline-none transition-colors hover:bg-muted hover:text-foreground data-[popup-open]:bg-muted data-[popup-open]:text-foreground disabled:opacity-50',
          triggerClassName,
        )}
      >
        <Icon className="h-3.5 w-3.5" />
        <span className="max-w-[140px] truncate">{label}</span>
        <ChevronDown className="h-3 w-3 opacity-60" />
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align={align}
        className="w-[19rem] max-w-[calc(100vw_-_1.5rem)] rounded-2xl border border-border/80 p-1.5 shadow-float sm:w-80"
      >
        {heading && (
          <div className="px-2 pb-1.5 pt-1 text-[11px] font-medium tracking-wide text-muted-foreground">
            {heading}
          </div>
        )}
        <DropdownMenuRadioGroup
          value={value}
          onValueChange={(next) => {
            const match = options.find((option) => option.value === next);
            if (match && !match.disabled) onChange(match.value);
          }}
          className="flex flex-col gap-1"
        >
          {options.map((option) => {
            const selected = option.value === value;
            const OptionIcon = option.icon;
            return (
              <DropdownMenuRadioItem
                key={option.value}
                value={option.value}
                disabled={option.disabled}
                closeOnClick
                className={cn(
                  'items-start gap-3 whitespace-normal rounded-xl border px-3 py-2.5 pr-9 transition-colors cursor-pointer [&_[data-slot=dropdown-menu-radio-item-indicator]]:right-3 [&_[data-slot=dropdown-menu-radio-item-indicator]]:top-3 [&_[data-slot=dropdown-menu-radio-item-indicator]]:text-primary',
                  selected
                    ? 'border-primary/40 bg-primary/[0.07]'
                    : 'border-transparent hover:border-border hover:bg-muted/60',
                )}
              >
                <span
                  className={cn(
                    'flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border',
                    selected
                      ? 'border-primary/30 bg-card text-primary'
                      : 'border-border/80 bg-card text-muted-foreground',
                  )}
                >
                  <OptionIcon className="h-4 w-4" />
                </span>
                <span className="min-w-0 flex-1">
                  <span className="flex items-center gap-1.5">
                    <span className="truncate text-[13px] font-medium text-foreground">
                      {option.title}
                    </span>
                    {option.badge}
                  </span>
                  {option.description && (
                    <span className="mt-0.5 block text-[11.5px] leading-relaxed text-muted-foreground">
                      {option.description}
                    </span>
                  )}
                </span>
              </DropdownMenuRadioItem>
            );
          })}
        </DropdownMenuRadioGroup>
        {footer && <div className="mt-1 border-t border-border/60 px-1 pt-1">{footer}</div>}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
