import { Suspense, lazy } from 'react';
import { Loader2 } from 'lucide-react';

import type { PdfOriginalViewProps } from './PdfOriginalViewImpl';

/**
 * 原版 PDF 视图的懒加载外壳。
 *
 * pdf.js 本体压缩后四百多 KB，worker 另算一个一兆多的文件；只有用户在阅读器里
 * 切到「原版 PDF」时才需要它们，不该跟着文库页一起下载。
 */
const Impl = lazy(() =>
  import('./PdfOriginalViewImpl').then((module) => ({ default: module.PdfOriginalViewImpl })),
);

export type { PdfJumpTarget, PdfOriginalViewProps } from './PdfOriginalViewImpl';

export function PdfOriginalView(props: PdfOriginalViewProps) {
  return (
    <Suspense
      fallback={
        <div className="flex h-full items-center justify-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-5 w-5 animate-spin text-primary" />
          <span>正在准备 PDF 阅读器…</span>
        </div>
      }
    >
      <Impl {...props} />
    </Suspense>
  );
}
