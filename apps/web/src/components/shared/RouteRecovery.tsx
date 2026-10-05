import { useRouteError } from 'react-router';
import { RefreshCw, Home, Layers } from 'lucide-react';
import { Button } from '@/components/ui/button';

export function RouteRecovery() {
  const error = useRouteError();
  const isModuleError =
    error instanceof Error &&
    /dynamically imported|Loading chunk|module script/i.test(error.message);

  return (
    <div
      role="alert"
      className="flex min-h-dvh items-center justify-center bg-background p-6"
    >
      <div className="w-full max-w-md rounded-2xl border border-border bg-card p-8 text-center shadow-sm">
        <div className="mx-auto mb-6 flex h-14 w-14 items-center justify-center rounded-2xl bg-primary/10 text-primary">
          <Layers className="h-6 w-6" />
        </div>
        <h1 className="text-xl font-semibold text-foreground">
          {isModuleError ? '应用已更新，请重新加载' : '这个页面暂时无法显示'}
        </h1>
        <p className="mt-3 text-sm leading-relaxed text-muted-foreground">
          {isModuleError
            ? '重新加载即可获取最新版本的页面。'
            : '可以重新加载页面，或回到工作空间继续使用。'}
        </p>
        <div className="mt-6 flex flex-wrap justify-center gap-3">
          <Button variant="outline" onClick={() => window.location.assign('/')}>
            <Home className="h-4 w-4" />
            回到工作空间
          </Button>
          <Button onClick={() => window.location.reload()}>
            <RefreshCw className="h-4 w-4" />
            重新加载
          </Button>
        </div>
      </div>
    </div>
  );
}
