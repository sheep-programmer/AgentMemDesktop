import { create } from 'zustand';

export type ThemeMode = 'light' | 'dark' | 'system';

interface ThemeState {
  theme: ThemeMode;
  resolvedTheme: 'light' | 'dark';
  setTheme: (theme: ThemeMode) => void;
}

function getSystemTheme(): 'light' | 'dark' {
  if (typeof window === 'undefined' || !window.matchMedia) return 'light';
  return window.matchMedia('(prefers-color-scheme: dark)').matches
    ? 'dark'
    : 'light';
}

function applyTheme(theme: ThemeMode) {
  const root = document.documentElement;
  root.style.colorScheme = theme === 'system' ? getSystemTheme() : theme;
  const resolved = theme === 'system' ? getSystemTheme() : theme;

  if (resolved === 'dark') {
    root.classList.add('dark');
  } else {
    root.classList.remove('dark');
  }

  return resolved;
}

export const useThemeStore = create<ThemeState>((set) => {
  let savedTheme: ThemeMode = 'system';
  try {
    const value = localStorage.getItem('agentmem_theme');
    if (value === 'light' || value === 'dark' || value === 'system')
      savedTheme = value;
  } catch {
    // 存储不可用时仍可正常打开和切换主题。
  }

  const initialResolved = applyTheme(savedTheme);

  if (typeof window !== 'undefined' && window.matchMedia) {
    window
      .matchMedia('(prefers-color-scheme: dark)')
      .addEventListener('change', () => {
        const current = useThemeStore.getState().theme;
        if (current === 'system') {
          const resolved = applyTheme('system');
          set({ resolvedTheme: resolved });
        }
      });
  }

  return {
    theme: savedTheme,
    resolvedTheme: initialResolved,
    setTheme: (theme: ThemeMode) => {
      try {
        localStorage.setItem('agentmem_theme', theme);
      } catch {
        /* 当前会话仍然生效。 */
      }
      const resolved = applyTheme(theme);
      set({ theme, resolvedTheme: resolved });
    },
  };
});
