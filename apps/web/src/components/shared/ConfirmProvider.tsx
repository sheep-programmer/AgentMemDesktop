import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { useLocation } from 'react-router';
import { AlertTriangle, CircleHelp } from 'lucide-react';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { cn } from '@/lib/utils';

export interface ConfirmOptions {
  title: string;
  description: string;
  confirmText?: string;
  destructive?: boolean;
}

type ConfirmRequest = (options: ConfirmOptions, signal: AbortSignal) => Promise<boolean>;
interface PendingConfirmation {
  options: ConfirmOptions;
  resolve: (accepted: boolean) => void;
  signal: AbortSignal;
  abort: () => void;
}

const ConfirmContext = createContext<ConfirmRequest | null>(null);

/** Each caller owns its request; navigation and unmount always dismiss with cancellation. */
export function useConfirm() {
  const request = useContext(ConfirmContext);
  const controller = useRef(new AbortController());
  useEffect(() => {
    const lifetime = new AbortController();
    controller.current = lifetime;
    return () => lifetime.abort();
  }, []);
  // Standalone feature tests and embedders can render a feature without the app shell.
  // The application always provides the themed dialog above.
  return useCallback(
    (options: ConfirmOptions) =>
      request
        ? request(options, controller.current.signal)
        : Promise.resolve(
            typeof globalThis.confirm === 'function' &&
              globalThis.confirm(`${options.title}\n\n${options.description}`),
          ),
    [request],
  );
}

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const location = useLocation();
  const pending = useRef<PendingConfirmation | null>(null);
  const [options, setOptions] = useState<ConfirmOptions | null>(null);
  const cancelButton = useRef<HTMLButtonElement>(null);

  const settle = useCallback((accepted: boolean) => {
    const current = pending.current;
    if (!current) return;
    pending.current = null;
    current.signal.removeEventListener('abort', current.abort);
    setOptions(null);
    current.resolve(accepted && !current.signal.aborted);
  }, []);

  const request = useCallback<ConfirmRequest>((next, signal) => {
    // A repeated click cannot replace the existing decision or execute twice.
    if (signal.aborted || pending.current) return Promise.resolve(false);
    return new Promise<boolean>((resolve) => {
      const abort = () => settle(false);
      pending.current = { options: next, signal, abort, resolve };
      signal.addEventListener('abort', abort, { once: true });
      setOptions(next);
    });
  }, [settle]);

  useEffect(() => { settle(false); }, [location.key, settle]);
  useEffect(() => () => {
    const current = pending.current;
    if (!current) return;
    pending.current = null;
    current.signal.removeEventListener('abort', current.abort);
    current.resolve(false);
  }, []);

  const destructive = options?.destructive ?? false;
  const Icon = destructive ? AlertTriangle : CircleHelp;
  return (
    <ConfirmContext.Provider value={request}>
      {children}
      <Dialog open={options !== null} onOpenChange={(open) => { if (!open) settle(false); }}>
        <DialogContent role="alertdialog" initialFocus={cancelButton} showCloseButton={false} className="max-w-md gap-6 p-6 sm:p-7">
          <DialogHeader className="gap-3">
            <div className={cn('mb-1 flex h-11 w-11 items-center justify-center rounded-xl border', destructive ? 'border-destructive/20 bg-destructive/10 text-destructive' : 'border-primary/20 bg-primary/10 text-primary')}>
              <Icon className="h-5 w-5" aria-hidden="true" />
            </div>
            <DialogTitle className="break-words text-lg leading-relaxed">{options?.title}</DialogTitle>
            <DialogDescription className="whitespace-pre-line break-words text-sm leading-relaxed">{options?.description}</DialogDescription>
          </DialogHeader>
          <DialogFooter className="gap-2">
            <Button ref={cancelButton} variant="outline" onClick={() => settle(false)}>取消</Button>
            <Button variant={destructive ? 'destructive' : 'default'} onClick={() => settle(true)}>{options?.confirmText || '确认继续'}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </ConfirmContext.Provider>
  );
}
