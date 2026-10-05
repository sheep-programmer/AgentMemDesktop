import React from 'react';
import { useThemeStore } from '@/stores/useThemeStore';
import { Sun, Moon, Laptop } from 'lucide-react';

export function AppearanceSection() {
  const { theme, setTheme } = useThemeStore();

  return (
    <div className="rounded-2xl border border-border bg-card p-6 space-y-5">
      <div>
        <h3 className="text-sm font-semibold text-foreground">外观</h3>
        <p className="text-xs text-muted-foreground mt-0.5">
          深色沉浸模式针对代码高亮与知识图谱做了专门对比度调优，浅色模式模拟纸质阅读质感。
        </p>
      </div>

      <div className="space-y-3 pt-1">
        <label className="text-xs font-medium text-foreground">配色主题</label>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          <div
            onClick={() => setTheme('dark')}
            className={`cursor-pointer rounded-xl border p-4 text-xs transition-all flex flex-col items-center gap-2 ${
              theme === 'dark'
                ? 'border-primary bg-primary/10 text-primary shadow-xs'
                : 'border-border bg-background hover:bg-muted/40 text-foreground'
            }`}
          >
            <Moon className="h-6 w-6" />
            <span className="font-semibold">深色模式</span>
            <span className="text-[10px] text-muted-foreground text-center">
              高精冷灰底色，低眩光适合长时间专业文献研读与多维推演
            </span>
          </div>

          <div
            onClick={() => setTheme('light')}
            className={`cursor-pointer rounded-xl border p-4 text-xs transition-all flex flex-col items-center gap-2 ${
              theme === 'light'
                ? 'border-primary bg-primary/10 text-primary shadow-xs'
                : 'border-border bg-background hover:bg-muted/40 text-foreground'
            }`}
          >
            <Sun className="h-6 w-6" />
            <span className="font-semibold">浅色模式</span>
            <span className="text-[10px] text-muted-foreground text-center">
              纸质米白底色，墨水质感对比度
            </span>
          </div>

          <div
            onClick={() => setTheme('system')}
            className={`cursor-pointer rounded-xl border p-4 text-xs transition-all flex flex-col items-center gap-2 ${
              theme === 'system'
                ? 'border-primary bg-primary/10 text-primary shadow-xs'
                : 'border-border bg-background hover:bg-muted/40 text-foreground'
            }`}
          >
            <Laptop className="h-6 w-6" />
            <span className="font-semibold">跟随系统</span>
            <span className="text-[10px] text-muted-foreground text-center">
              自动匹配操作系统浅色 / 深色外观
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}
