import React, { useMemo, type ComponentProps } from 'react';
import { Streamdown, defaultRemarkPlugins } from 'streamdown';
import type { CitationMarker } from '@/lib/api/types.temp';
import { Tooltip, TooltipTrigger, TooltipContent } from '@/components/ui/tooltip';
import { FileText } from 'lucide-react';
import { citationLabel } from '@/lib/markdown';
import { describeLocation } from '@/features/chat/lib/citations';

/**
 * 全站统一的 Markdown 渲染器（实现体）。
 *
 * 对外别直接 import 这个文件：它会把 streamdown + katex + shiki + mermaid 一起拖进来
 * （压缩后约 1.3MB，占首包的绝大部分）。统一走 `MarkdownView.tsx` 那层懒加载。
 *
 * - 开启 remark-math 的 singleDollarTextMath，让 `$IC_{50}$` 这类行内公式不被当成正文。
 * - 正文里的 `[^c1]` / `[1]` 转成可悬停预览、可点击下钻的引用芯片。
 * - 表格、代码块、引用块与浅色 / 深色两套主题对齐。
 * - 支持切片阅读器的锚点标记（`#chunk-anchor:`）。
 */
type RemarkPlugins = ComponentProps<typeof Streamdown>['remarkPlugins'];

/**
 * 过滤掉不该落到 DOM 上的属性。
 *
 * streamdown 会把 hast 节点以 `node` 传入自定义组件，直接 `{...props}` 展开会让 React
 * 把它字符串化成一个 `node="[object Object]"` 属性写进 HTML。
 */
function domProps<T extends { node?: unknown }>(props: T): Omit<T, 'node'> {
  const rest = { ...props };
  delete rest.node;
  return rest;
}

const REMARK_PLUGINS = Object.entries(defaultRemarkPlugins).map(([key, plugin]) => {
  if (key === 'math' && Array.isArray(plugin)) {
    return [plugin[0], { ...(plugin[1] as object), singleDollarTextMath: true }];
  }
  return plugin;
}) as RemarkPlugins;

function preprocessCitations(content: string): string {
  if (!content) return '';
  // 避免替换代码块与行内代码内部的标记
  const parts = content.split(/(```[\s\S]*?```|`[^`\n]+`)/g);
  return parts
    .map((part, index) => {
      if (index % 2 === 1) return part;
      return part
        .replace(/\[\^([a-zA-Z0-9_-]+)\]/g, '[$1](#cite:$1)')
        .replace(/(?<=\s|^|[\u4e00-\u9fa5，。！？、；：])\[(c?[0-9]+)\](?!\()/g, '[$1](#cite:$1)');
    })
    .join('');
}

export interface MarkdownViewProps {
  children: string;
  className?: string;
  citations?: CitationMarker[];
  onCitationClick?: (chunkId: string) => void;
}

export function MarkdownViewImpl({
  children,
  className,
  citations,
  onCitationClick,
}: MarkdownViewProps) {
  const processedContent = useMemo(() => {
    return preprocessCitations(children);
  }, [children]);

  /**
   * Streamdown 按内容分块 memo 已渲染的块。引用事件是流式期间逐个（mock 里是最后
   * 一次性）到达的：先渲染的块被缓存住，`a` 组件闭包里还是旧的 citations——
   * 实测同一条回答前面的标记停在「查无此引用」的灰色 [cN] 兜底，后面的却是数字
   * 芯片。citation 集合变化时用 key 重挂载，让所有块按最新的引用表重算。
   * 引用一条回答通常只有个位数，重挂载代价可忽略。
   */
  const citationKey = (citations ?? []).map((c) => c.marker).join(',');

  const components: ComponentProps<typeof Streamdown>['components'] = useMemo(
    () => ({
      table: ({ children: tableChildren, ...props }) => (
        <div className="my-3 w-full overflow-x-auto rounded-lg border border-border/80 shadow-2xs bg-card/40">
          <table className="w-full text-left text-xs border-collapse divide-y divide-border/60" {...domProps(props)}>
            {tableChildren}
          </table>
        </div>
      ),
      thead: ({ children: theadChildren, ...props }) => (
        <thead className="bg-muted/70 text-foreground font-semibold text-[11.5px]" {...domProps(props)}>
          {theadChildren}
        </thead>
      ),
      tbody: ({ children: tbodyChildren, ...props }) => (
        <tbody className="divide-y divide-border/40 bg-card/20" {...domProps(props)}>
          {tbodyChildren}
        </tbody>
      ),
      tr: ({ children: trChildren, ...props }) => (
        <tr className="transition-colors hover:bg-muted/40" {...domProps(props)}>
          {trChildren}
        </tr>
      ),
      th: ({ children: thChildren, ...props }) => (
        <th className="px-3 py-2 text-foreground font-semibold border-r border-border/30 last:border-r-0 whitespace-nowrap" {...domProps(props)}>
          {thChildren}
        </th>
      ),
      td: ({ children: tdChildren, ...props }) => (
        // 单元格用正文字体：中文表格用等宽字体既难读也对不齐，等宽只留给代码
        <td className="px-3 py-2 text-foreground/90 border-r border-border/20 last:border-r-0 text-[12px] leading-relaxed align-top" {...domProps(props)}>
          {tdChildren}
        </td>
      ),
      pre: ({ children: preChildren, ...props }) => (
        <pre className="my-3 overflow-x-auto rounded-lg border border-border/80 bg-muted/50 p-3.5 font-mono text-xs text-foreground" {...domProps(props)}>
          {preChildren}
        </pre>
      ),
      blockquote: ({ children: bqChildren, ...props }) => (
        <blockquote className="my-3 border-l-3 border-primary/60 bg-muted/30 px-3.5 py-2 text-sm italic text-foreground/80 rounded-r-md" {...domProps(props)}>
          {bqChildren}
        </blockquote>
      ),
      a: ({ href, children: linkChildren, ...props }) => {
        if (href?.startsWith('#chunk-anchor:')) {
          const chunkId = href.replace('#chunk-anchor:', '');
          return (
            <span
              id={`chunk-anchor-${chunkId}`}
              data-chunk-id={chunkId}
              className="inline-block h-0 w-0 opacity-0 pointer-events-none"
            />
          );
        }

        if (href?.startsWith('#cite:')) {
          const marker = href.replace('#cite:', '');
          const citation = citations?.find(
            (c) =>
              c.marker === marker ||
              c.marker === `c${marker}` ||
              c.marker.replace(/^c/i, '') === marker.replace(/^c/i, ''),
          );

          // 匹配不到真实引用的标记，不做成引用芯片。
          //
          // 提示词规定的协议是 `[^cN]`，但模型会跑偏——实测见过它写 `[^e2]`，
          // 后端的 MARKER_PATTERN 只认 `[^cN]`，于是既没解析也没从正文里剥掉。
          // 此前这里照样渲染成一枚可点的芯片，点下去传的是一个不存在的 chunk id，
          // 什么也不会发生：一个看起来能下钻、实际点不开的引用，
          // 比直接显示原文更糟。现在退化成普通文本，如实呈现模型写了什么。
          if (!citation) {
            return <span className="text-muted-foreground">[{marker}]</span>;
          }

          return (
            <Tooltip>
              {/* 引用角标：右上角小标，不撑行高、不打断阅读节奏；
                  悬停出出处预览，点击仍在证据栏定位 */}
              <TooltipTrigger
                onClick={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  onCitationClick?.(citation?.chunk_id || marker);
                }}
                aria-label={`引用 ${citationLabel(marker)}：${citation?.document_title || '来源文档'}`}
                className="cite-chip mx-[1px] inline-flex h-[15px] min-w-[15px] -translate-y-[1px] items-center justify-center rounded-full bg-primary/12 px-[4px] align-middle font-mono text-[9.5px] font-semibold leading-none text-primary cursor-pointer select-none transition-colors hover:bg-primary hover:text-primary-foreground"
              >
                {citationLabel(marker)}
              </TooltipTrigger>
              <TooltipContent
                side="top"
                align="center"
                className="z-50 max-w-xs rounded-lg border border-border/80 bg-popover p-2.5 text-xs text-popover-foreground shadow-lg backdrop-blur-md"
              >
                <div className="flex items-center gap-1.5 font-medium text-foreground">
                  <FileText className="h-3.5 w-3.5 text-primary shrink-0" />
                  <span className="truncate">
                    {citation?.document_title || `引用 ${citationLabel(marker)}`}
                  </span>
                </div>
                {describeLocation(citation) && (
                  <div className="mt-0.5 text-[11px] text-muted-foreground">
                    {describeLocation(citation)}
                  </div>
                )}
                {citation?.snippet && (
                  <div className="mt-1 max-h-40 overflow-y-auto text-[11px] text-muted-foreground leading-relaxed prose prose-sm dark:prose-invert max-w-none prose-p:my-0.5 prose-table:my-0.5 prose-pre:my-0.5">
                    <MarkdownViewImpl>{citation.snippet}</MarkdownViewImpl>
                  </div>
                )}
                <div className="mt-2 border-t border-border/40 pt-1.5 text-[10.5px] text-muted-foreground">
                  引用 {citationLabel(marker)} · 点击在证据栏定位
                </div>
              </TooltipContent>
            </Tooltip>
          );
        }

        return (
          <a
            href={href}
            target="_blank"
            rel="noreferrer"
            className="text-primary underline underline-offset-4 hover:opacity-80 break-words"
            {...domProps(props)}
          >
            {linkChildren}
          </a>
        );
      },
    }),
    [citations, onCitationClick],
  );

  return (
    <Streamdown
      key={citationKey}
      className={className}
      remarkPlugins={REMARK_PLUGINS}
      components={components}
    >
      {processedContent}
    </Streamdown>
  );
}
