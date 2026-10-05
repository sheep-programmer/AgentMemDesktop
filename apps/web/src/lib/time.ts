/**
 * 格式化相对时间（如 "刚刚"、"5 分钟前"、"2 小时前"、"2 天前"）
 */
export function formatRelativeTime(timestamp: number | null | undefined): string {
  if (!timestamp) return '未知时间';
  const now = Date.now();
  const diffMs = now - timestamp;
  if (diffMs < 0) return '刚刚';
  const diffSec = Math.floor(diffMs / 1000);
  if (diffSec < 60) return '刚刚';
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin} 分钟前`;
  const diffHour = Math.floor(diffMin / 60);
  if (diffHour < 24) return `${diffHour} 小时前`;
  const diffDay = Math.floor(diffHour / 24);
  if (diffDay < 30) return `${diffDay} 天前`;
  const diffMonth = Math.floor(diffDay / 30);
  if (diffMonth < 12) return `${diffMonth} 个月前`;
  const diffYear = Math.floor(diffDay / 365);
  return `${diffYear} 年前`;
}
