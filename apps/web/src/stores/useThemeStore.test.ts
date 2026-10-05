import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

beforeEach(() => {
  vi.resetModules();
  document.documentElement.classList.remove('dark');
  vi.stubGlobal('matchMedia', () => ({
    matches: false,
    addEventListener: vi.fn(),
  }));
});
afterEach(() => vi.unstubAllGlobals());

describe('theme preferences', () => {
  it('ignores invalid saved values and follows the system', async () => {
    vi.stubGlobal('localStorage', {
      getItem: () => 'invalid',
      setItem: vi.fn(),
    });
    const { useThemeStore } = await import('./useThemeStore');
    expect(useThemeStore.getState()).toMatchObject({
      theme: 'system',
      resolvedTheme: 'light',
    });
    expect(document.documentElement.style.colorScheme).toBe('light');
  });

  it('still starts and switches when persistence is blocked', async () => {
    vi.stubGlobal('localStorage', {
      getItem: () => {
        throw new Error('blocked');
      },
      setItem: () => {
        throw new Error('blocked');
      },
    });
    const { useThemeStore } = await import('./useThemeStore');
    useThemeStore.getState().setTheme('dark');
    expect(useThemeStore.getState().resolvedTheme).toBe('dark');
    expect(document.documentElement.classList.contains('dark')).toBe(true);
  });

  it('updates system preferences while preserving a manual override', async () => {
    vi.stubGlobal('localStorage', {
      getItem: () => 'system',
      setItem: vi.fn(),
    });
    const media = new EventTarget();
    const query = {
      matches: false,
      addEventListener: media.addEventListener.bind(media),
    };
    vi.stubGlobal('matchMedia', () => query);
    const { useThemeStore } = await import('./useThemeStore');
    query.matches = true;
    media.dispatchEvent(new Event('change'));
    expect(useThemeStore.getState().resolvedTheme).toBe('dark');
    useThemeStore.getState().setTheme('light');
    media.dispatchEvent(new Event('change'));
    expect(useThemeStore.getState().resolvedTheme).toBe('light');
  });
});
