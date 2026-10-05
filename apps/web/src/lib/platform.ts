/**
 * 平台差异：快捷键提示在 macOS 上写 ⌘，在 Windows / Linux 上写 Ctrl。
 *
 * 实际按键监听两边都认（见 RootLayout），只有提示文案需要换：Windows 用户看到 ⌘K
 * 会去找一个键盘上没有的键。
 */
export const isMac =
  typeof navigator !== 'undefined' &&
  /mac|iphone|ipad/i.test(
    (navigator as Navigator & { userAgentData?: { platform?: string } }).userAgentData?.platform ||
      navigator.platform ||
      '',
  );

/** `⌘K` → Windows 上 `Ctrl+K`；`⌘\` → `Ctrl+\`。 */
export function formatShortcut(shortcut: string): string {
  return isMac ? shortcut : shortcut.replace(/⌘\s?/g, 'Ctrl+');
}

export const modKey = isMac ? '⌘' : 'Ctrl+';
