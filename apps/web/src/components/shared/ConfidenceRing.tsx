import React from 'react';
import { cn } from '@/lib/utils';

interface ConfidenceRingProps {
  value: number;
  /**
   * value 的满分。置信度是 0~1（默认），专家度与进度是 0~100。
   * 此前靠「≤1 就当 0~1」猜：专家度 0.8 分会被画成 80 分。
   */
  max?: 1 | 100;
  size?: number;
  strokeWidth?: number;
  showText?: boolean;
  className?: string;
}

export function ConfidenceRing({
  value,
  max = 1,
  size = 28,
  strokeWidth = 3,
  showText = true,
  className,
}: ConfidenceRingProps) {
  const normalized = Math.max(0, Math.min(100, Math.round(max === 1 ? value * 100 : value)));
  const radius = (size - strokeWidth) / 2;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference - (normalized / 100) * circumference;

  let colorClass = 'text-primary';
  if (normalized >= 80) colorClass = 'text-accent-insight';
  else if (normalized >= 50) colorClass = 'text-accent-ai';
  else colorClass = 'text-accent-warn';

  return (
    <div
      className={cn('relative inline-flex items-center justify-center font-mono select-none', className)}
      style={{ width: size, height: size }}
    >
      <svg width={size} height={size} className="-rotate-90">
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke="currentColor"
          strokeWidth={strokeWidth}
          className="text-muted/60"
        />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke="currentColor"
          strokeWidth={strokeWidth}
          strokeDasharray={circumference}
          strokeDashoffset={offset}
          strokeLinecap="round"
          className={cn('transition-all duration-500 ease-out', colorClass)}
        />
      </svg>
      {showText && (
        <span className="absolute text-[10px] font-medium tracking-tighter">
          {normalized}
        </span>
      )}
    </div>
  );
}
