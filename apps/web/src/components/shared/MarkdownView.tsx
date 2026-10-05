import { Suspense, lazy } from 'react';

import type { MarkdownViewProps } from './MarkdownViewImpl';

/**
 * Markdown 渲染器的懒加载外壳。
 *
 * 渲染器本身带着 streamdown + katex + shiki + mermaid，压缩后 1.3MB——六个页面都
 * 引它，打包器于是把这一坨放进了入口 chunk，连「只想看看设置页」都要先下完。
 * 拆成按需加载之后，首屏只在真正要渲染 Markdown 时才去取这一块。
 *
 * 等待期间退化成纯文本而不是骨架屏：对话是流式的，空白一闪比字体变一下更扎眼，
 * 而这里的正文本来就是可读的文字。
 */
const Impl = lazy(() =>
  import('./MarkdownViewImpl').then((module) => ({ default: module.MarkdownViewImpl })),
);

export type { MarkdownViewProps };

export function MarkdownView(props: MarkdownViewProps) {
  return (
    <Suspense
      fallback={
        <div className={props.className ?? 'whitespace-pre-wrap leading-relaxed'}>
          {props.children}
        </div>
      }
    >
      <Impl {...props} />
    </Suspense>
  );
}
