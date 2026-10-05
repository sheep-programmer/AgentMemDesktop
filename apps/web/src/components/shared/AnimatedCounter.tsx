import React, { useEffect, useState, useRef } from 'react';

interface AnimatedCounterProps {
  value: number;
  duration?: number;
  decimals?: number;
  className?: string;
}

export function AnimatedCounter({
  value,
  duration = 800,
  decimals = 1,
  className = '',
}: AnimatedCounterProps) {
  const [displayValue, setDisplayValue] = useState(value);
  const prevValueRef = useRef(value);

  useEffect(() => {
    const start = prevValueRef.current;
    const end = value;
    prevValueRef.current = value;
    if (start === end) {
      setDisplayValue(end);
      return;
    }

    const startTime = performance.now();
    let animId: number;

    const tick = (now: number) => {
      const elapsed = now - startTime;
      const progress = Math.min(elapsed / duration, 1);
      const eased = 1 - Math.pow(1 - progress, 3);
      const current = start + (end - start) * eased;
      setDisplayValue(current);

      if (progress < 1) {
        animId = requestAnimationFrame(tick);
      } else {
        setDisplayValue(end);
      }
    };

    animId = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(animId);
  }, [value, duration]);

  return (
    <span className={className}>
      {displayValue.toFixed(decimals)}
    </span>
  );
}
