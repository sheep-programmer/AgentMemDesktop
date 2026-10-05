import { useCallback, useEffect, useRef } from 'react';

export interface AsyncTask {
  signal: AbortSignal;
  current: () => boolean;
  finish: () => boolean;
}

/** One owner per task. A previous request cannot publish after navigation or a new run. */
export function useAsyncTask(scope: string | null) {
  const mounted = useRef(true);
  const active = useRef<AbortController | null>(null);
  const currentScope = useRef(scope);
  currentScope.current = scope;
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      active.current?.abort();
      active.current = null;
    };
  }, [scope]);

  const start = useCallback((): AsyncTask | null => {
    if (!mounted.current || active.current) return null;
    const controller = new AbortController();
    const owner = currentScope.current;
    active.current = controller;
    const current = () =>
      mounted.current &&
      active.current === controller &&
      !controller.signal.aborted &&
      currentScope.current === owner;
    return {
      signal: controller.signal,
      current,
      finish: () => {
        const valid = current();
        if (active.current === controller) active.current = null;
        return valid;
      },
    };
  }, []);
  const cancel = useCallback(() => {
    active.current?.abort();
    active.current = null;
  }, []);
  return { start, cancel, running: () => Boolean(active.current) };
}
