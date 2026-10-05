import React, { useState, useRef, useMemo, useCallback, useEffect } from 'react';
import ForceGraph2D, { type ForceGraphMethods } from 'react-force-graph-2d';
import type { KnowledgeGraphData, EntityNode } from '@/lib/api/types.temp';
import { useThemeStore } from '@/stores/useThemeStore';
import { Search, X, Network, ZoomIn, ZoomOut, Maximize2, Tag, Info } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

interface KnowledgeGraphViewProps {
  data: KnowledgeGraphData;
}

/** 超过这个节点数就降级渲染（关光晕、关常驻标签）。 */
const DENSE_NODE_COUNT = 120;

/** 类型筛选默认只露出最常见的几种 */
const TYPE_PREVIEW = 8;

export function KnowledgeGraphView({ data }: KnowledgeGraphViewProps) {
  const { resolvedTheme } = useThemeStore();
  const [selectedNode, setSelectedNode] = useState<EntityNode | null>(null);
  const [hoverNode, setHoverNode] = useState<EntityNode | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  // 空集 = 不过滤。类型不能写死：实体类型由抽取模型按领域自由生成（靶点 / 酶 /
  // 理化性质 / 漏洞 …），写死成几个英文键会把所有节点都过滤掉，图画出来是空的。
  const [activeTypeFilters, setActiveTypeFilters] = useState<Set<string>>(new Set());
  // 实体类型由抽取模型按领域自由生成，实测一个空间有 45 种：全铺开会盖住半张图
  const [showAllTypes, setShowAllTypes] = useState(false);
  const [showLabels, setShowLabels] = useState(true);

  const fgRef = useRef<ForceGraphMethods | undefined>(undefined);
  const repaintTimerRef = useRef<number | undefined>(undefined);

  useEffect(() => {
    return () => window.clearTimeout(repaintTimerRef.current);
  }, []);
  const containerRef = useRef<HTMLDivElement>(null);
  const [dimensions, setDimensions] = useState({ width: 1, height: 650 });

  // 类型 → 颜色的稳定映射：类型名来自模型，先按已知领域词给固定色，其余按名字散列到
  // 同一套调色板上，保证同一个类型每次打开颜色一致
  const nodeColorMap = useMemo(() => {
    const palette = [
      'oklch(0.63 0.22 25)',
      'oklch(0.62 0.20 270)',
      'oklch(0.65 0.18 240)',
      'oklch(0.68 0.18 155)',
      'oklch(0.72 0.18 75)',
      'oklch(0.66 0.19 330)',
      'oklch(0.64 0.17 200)',
    ];
    const known: Record<string, string> = {
      靶点: palette[0],
      化合物: palette[1],
      突变: palette[2],
      通路: palette[3],
      实验: palette[4],
      酶: palette[6],
      理化性质: palette[5],
    };
    const mapping: Record<string, string> = {};
    const types = [...new Set(data.nodes.map((node) => node.type))].sort();
    types.forEach((type, index) => {
      mapping[type] = known[type] ?? palette[index % palette.length];
    });
    return mapping;
  }, [data.nodes]);

  const isMatchingSearch = useCallback(
    (name: string) => {
      if (!searchQuery.trim()) return true;
      return name.toLowerCase().includes(searchQuery.trim().toLowerCase());
    },
    [searchQuery],
  );

  // 统计当前加载并在图谱中呈现的各类型节点数（严格基于 data.nodes，不误用 total_nodes）
  const displayedTypeCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const node of data.nodes) {
      counts[node.type] = (counts[node.type] || 0) + 1;
    }
    return counts;
  }, [data.nodes]);

  // Filter nodes & links based on active type filters
  const filteredData = useMemo(() => {
    const validNodeIds = new Set<string>();
    const nodes = data.nodes.filter((node) => {
      if (activeTypeFilters.size > 0 && !activeTypeFilters.has(node.type)) {
        return false;
      }
      validNodeIds.add(node.id);
      return true;
    });

    const edges = data.edges.filter(
      (edge) => validNodeIds.has(edge.src) && validNodeIds.has(edge.dst),
    );

    return { nodes, edges };
  }, [data, activeTypeFilters]);

  // Compute 1-hop neighbor set for Obsidian-style hover focus
  const { highlightNodes, highlightLinks } = useMemo(() => {
    const nodes = new Set<string>();
    const links = new Set<string>();

    if (hoverNode) {
      nodes.add(hoverNode.id);
      filteredData.edges.forEach((edge, idx) => {
        if (edge.src === hoverNode.id || edge.dst === hoverNode.id) {
          links.add(`link-${idx}`);
          nodes.add(edge.src);
          nodes.add(edge.dst);
        }
      });
    }

    return { highlightNodes: nodes, highlightLinks: links };
  }, [hoverNode, filteredData.edges]);

  const formattedGraphData = useMemo(() => {
    return {
      nodes: filteredData.nodes.map((node) => ({
        ...node,
        // 后端把提及次数放在 weight 里（没有 mention_count 字段）：此前读 mention_count，
        // 所有节点被压成同一大小。开方缩放，提及很多的节点不至于大到盖住整张图
        val: 4 + Math.sqrt(Math.max(node.weight ?? 0, 0)) * 3,
        color: nodeColorMap[node.type] || 'oklch(0.62 0.20 270)',
      })),
      links: filteredData.edges.map((edge, idx) => ({
        id: `link-${idx}`,
        source: edge.src,
        target: edge.dst,
        name: edge.predicate,
        value: edge.weight,
      })),
    };
  }, [filteredData]);

  // 节点一多就降级渲染：每个节点每帧都要画光晕外环 + 实心核心 + 一段文字，
  // 文字排版又是这里最贵的一步。上百个节点时先把光晕与常驻标签关掉——图还是那张图，
  // 悬停、选中、搜索命中仍然带标签，而帧率不至于掉到拖不动。
  const isDense = formattedGraphData.nodes.length > DENSE_NODE_COUNT;
  const labelsVisible = showLabels && !isDense;

  const hasNodes = formattedGraphData.nodes.length > 0;
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const measure = () => {
      const width = Math.max(1, container.clientWidth);
      const height = Math.max(1, container.clientHeight);
      setDimensions((previous) => previous.width === width && previous.height === height ? previous : { width, height });
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(container);
    return () => observer.disconnect();
  }, [hasNodes]);


  const toggleTypeFilter = (type: string) => {
    setActiveTypeFilters((prev) => {
      const next = new Set(prev);
      if (next.has(type)) {
        if (next.size > 1) next.delete(type);
      } else {
        next.add(type);
      }
      return next;
    });
  };

  const handleZoomIn = () => {
    if (fgRef.current) {
      const current = fgRef.current.zoom();
      fgRef.current.zoom((current || 1) * 1.3, 300);
    }
  };

  const handleZoomOut = () => {
    if (fgRef.current) {
      const current = fgRef.current.zoom();
      fgRef.current.zoom((current || 1) / 1.3, 300);
    }
  };

  const handleFitView = () => {
    if (fgRef.current) {
      fgRef.current.zoomToFit(400, 40);
    }
  };

  if (data.nodes.length === 0) {
    return (
      <div className="flex h-[550px] w-full flex-col items-center justify-center rounded-xl border border-dashed border-border bg-card/40 p-8 text-center select-none">
        <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-muted text-muted-foreground mb-3">
          <Network className="h-6 w-6" />
        </div>
        <h4 className="text-sm font-semibold text-foreground">暂无结构化实体图谱</h4>
        <p className="mt-1 text-xs text-muted-foreground max-w-sm leading-relaxed">
          知识库导入资料或执行进化后，系统会自动抽取其中的实体与关系，并在此绘制关联图。
        </p>
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      role="region"
      aria-label="知识图谱"
      className="relative h-[650px] w-full overflow-hidden rounded-xl border border-border bg-background shadow-xs select-none"
    >
      {/* 搜索与图例筛选浮层 */}
      <div className="absolute left-4 top-4 z-10 flex flex-col gap-2.5 max-w-sm">
        <div className="relative w-64">
          <Search className="absolute left-2.5 top-2.5 h-3.5 w-3.5 text-muted-foreground" />
          <input
            type="text"
            placeholder="在图谱中搜索实体..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="w-full rounded-lg border border-border bg-card/90 py-1.5 pl-8 pr-3 text-xs text-foreground placeholder:text-muted-foreground backdrop-blur-md outline-none focus:border-primary/50 focus:ring-1 focus:ring-primary/20 shadow-xs transition-all"
          />
        </div>

        {/* 裁剪说明如实提示：仅在 truncated === true 时显示，未裁剪时不占地方 */}
        {data.truncated && (
          <div className="flex items-start gap-1.5 rounded-lg border border-amber-500/30 bg-card/90 p-2 text-[11px] text-muted-foreground backdrop-blur-md shadow-2xs leading-relaxed">
            <Info className="h-3.5 w-3.5 text-amber-700 dark:text-amber-400 shrink-0 mt-0.5" />
            <div>
              已显示 {data.nodes.length} / {data.total_nodes ?? data.nodes.length} 个实体（按提及次数保留前 {data.nodes.length} 个），可用类型筛选或搜索缩小范围。
            </div>
          </div>
        )}

        {/* 降级提示：省掉的是装饰，不是信息——说清楚哪些还在 */}
        {isDense && (
          <div className="flex items-start gap-1.5 rounded-lg border border-border/60 bg-card/90 p-2 text-[11px] text-muted-foreground backdrop-blur-md shadow-2xs leading-relaxed">
            <Info className="h-3.5 w-3.5 text-primary shrink-0 mt-0.5" />
            <div>
              节点较多（{formattedGraphData.nodes.length} 个），已自动简化渲染：隐藏常驻标签与光晕。
              悬停、选中与搜索命中的节点仍然带标签。
            </div>
          </div>
        )}

        {/* 交互式图例（点击过滤特定类型，对标 Obsidian Graph 交互，数字为当前显示的节点数） */}
        <div className="flex flex-col gap-1.5 rounded-lg border border-border/60 bg-card/85 p-2 text-[10.5px] text-muted-foreground backdrop-blur-md shadow-2xs">
          <div className="flex items-center justify-between gap-2 px-0.5 text-[10px] text-muted-foreground font-medium">
            <span>按类型筛选（数字为当前显示）</span>
            {activeTypeFilters.size > 0 && (
              <button
                type="button"
                onClick={() => setActiveTypeFilters(new Set())}
                className="text-primary hover:underline cursor-pointer"
              >
                清除筛选
              </button>
            )}
          </div>
          <div className="flex max-h-40 flex-wrap gap-1.5 overflow-y-auto">
            {Object.entries(displayedTypeCounts)
              .sort((left, right) => right[1] - left[1])
              .filter(([type], index) => showAllTypes || index < TYPE_PREVIEW || activeTypeFilters.has(type))
              .map(([type, count]) => {
              const label = type;
              // 没有任何筛选时就是「全部显示」，按钮不该全部显得像被禁用
              const isActive = activeTypeFilters.size === 0 || activeTypeFilters.has(type);
              return (
                <button
                  key={type}
                  type="button"
                  onClick={() => toggleTypeFilter(type)}
                  title={`当前显示 ${count} 个${label}`}
                  className={cn(
                    'flex items-center gap-1.5 rounded px-2 py-0.5 transition-all cursor-pointer border',
                    isActive
                      ? 'border-border/60 bg-muted/50 text-foreground font-medium'
                      : 'border-transparent opacity-40 hover:opacity-75',
                  )}
                >
                  <span
                    className="h-2 w-2 rounded-full"
                    style={{ backgroundColor: nodeColorMap[type] }}
                  />
                  <span>{label}</span>
                  <span className="font-mono text-[9.5px] opacity-70">({count})</span>
                </button>
              );
            })}
          </div>
          {Object.keys(displayedTypeCounts).length > TYPE_PREVIEW && (
            <button
              type="button"
              onClick={() => setShowAllTypes((value) => !value)}
              className="self-start px-0.5 text-[10px] text-primary hover:underline cursor-pointer"
            >
              {showAllTypes
                ? '收起'
                : `展开全部 ${Object.keys(displayedTypeCounts).length} 种类型`}
            </button>
          )}
        </div>
      </div>

      {/* 右上角：Obsidian 式视图操作控制组 */}
      <div className="absolute right-4 top-4 z-10 flex items-center gap-1 rounded-lg border border-border/70 bg-card/85 p-1 shadow-2xs backdrop-blur-md">
        <Button
          variant="ghost"
          size="icon"
          onClick={handleZoomIn}
          className="h-7 w-7 text-muted-foreground hover:text-foreground cursor-pointer"
          title="放大"
        >
          <ZoomIn className="h-3.5 w-3.5" />
        </Button>
        <Button
          variant="ghost"
          size="icon"
          onClick={handleZoomOut}
          className="h-7 w-7 text-muted-foreground hover:text-foreground cursor-pointer"
          title="缩小"
        >
          <ZoomOut className="h-3.5 w-3.5" />
        </Button>
        <Button
          variant="ghost"
          size="icon"
          onClick={handleFitView}
          className="h-7 w-7 text-muted-foreground hover:text-foreground cursor-pointer"
          title="适应画布"
        >
          <Maximize2 className="h-3.5 w-3.5" />
        </Button>
        <div className="h-4 w-px bg-border/60 mx-0.5" />
        <Button
          variant="ghost"
          size="sm"
          onClick={() => setShowLabels((prev) => !prev)}
          disabled={isDense}
          className={cn(
            'h-7 px-2 text-[11px] gap-1 cursor-pointer',
            labelsVisible ? 'text-primary font-medium' : 'text-muted-foreground',
          )}
          title={
            isDense
              ? `节点超过 ${DENSE_NODE_COUNT} 个，已自动隐藏常驻标签（悬停、选中、搜索命中仍显示）`
              : '切换文字标签'
          }
        >
          <Tag className="h-3 w-3" />
          <span>标签</span>
        </Button>
      </div>

      {/* 2D 力导向图谱。
          力导向布局冷却后渲染循环会停下，此时悬停不再触发重绘——图上会一直停在
          上一次的「邻居高亮、其余变暗」状态。所以指针进入时恢复渲染循环、离开时
          清掉高亮并暂停，既不卡住也不白烧 CPU。 */}
      <div
        className="absolute inset-0"
        onMouseEnter={() => {
          fgRef.current?.resumeAnimation();
        }}
        onMouseLeave={() => {
          setHoverNode(null);
          fgRef.current?.resumeAnimation();
          window.clearTimeout(repaintTimerRef.current);
          repaintTimerRef.current = window.setTimeout(() => {
            fgRef.current?.pauseAnimation();
          }, 300);
        }}
      >
      <ForceGraph2D
        ref={fgRef}
        width={dimensions.width}
        height={dimensions.height}
        graphData={formattedGraphData}
        // 引擎冷却后力导向循环会停下，悬停检测随之失效：图会停在上一次的
        // 「邻居高亮、其余变暗」状态，指针移开也不恢复。这里让引擎一直活着，
        // 指针离开画布时再暂停渲染循环（见外层容器）。
        cooldownTime={Number.POSITIVE_INFINITY}
        backgroundColor={resolvedTheme === 'dark' ? 'oklch(0.16 0.015 260)' : 'oklch(0.98 0.005 260)'}
        nodeRelSize={4}
        linkColor={(link) => {
          const l = link as { id?: string };
          const isLinkHighlighted = hoverNode ? highlightLinks.has(l.id || '') : false;
          if (hoverNode) {
            return isLinkHighlighted
              ? 'var(--primary)'
              : resolvedTheme === 'dark'
                ? 'rgba(255,255,255,0.05)'
                : 'rgba(0,0,0,0.04)';
          }
          return resolvedTheme === 'dark' ? 'rgba(255,255,255,0.16)' : 'rgba(0,0,0,0.12)';
        }}
        linkWidth={(link) => {
          const l = link as { id?: string };
          return hoverNode && highlightLinks.has(l.id || '') ? 2.5 : 1.2;
        }}
        onNodeHover={(node) => {
          setHoverNode((node as unknown as EntityNode) || null);
        }}
        nodeCanvasObject={(node, ctx, globalScale) => {
          const n = node as unknown as EntityNode & {
            x: number;
            y: number;
            val: number;
            color: string;
          };
          const label = n.name;
          const isSearchMatch = isMatchingSearch(n.name);
          const isHoverFocused = hoverNode ? highlightNodes.has(n.id) : true;
          const isSelected = selectedNode?.id === n.id;

          const alpha = !isSearchMatch ? 0.08 : isHoverFocused ? 1.0 : 0.15;
          const outerRadius = n.val + (isSelected ? 5 : isHoverFocused && hoverNode ? 4 : 2);

          // 绘制光晕外环。降级模式下只给「选中 / 悬停邻居」画，其余省掉：
          // 这一圈是纯装饰，而它在密集图里是与节点数同量级的额外绘制
          if (!isDense || isSelected || (hoverNode && isHoverFocused)) {
            ctx.beginPath();
            ctx.arc(n.x, n.y, outerRadius, 0, 2 * Math.PI, false);
            ctx.fillStyle = n.color;
            ctx.globalAlpha = isSelected ? 0.4 : isHoverFocused ? 0.25 : 0.04;
            ctx.fill();
          }

          // 绘制实体中心核心
          ctx.beginPath();
          ctx.arc(n.x, n.y, n.val, 0, 2 * Math.PI, false);
          ctx.fillStyle = n.color;
          ctx.globalAlpha = alpha;
          ctx.fill();

          // 绘制文字标签
          if (labelsVisible || isSelected || isSearchMatch || (hoverNode && highlightNodes.has(n.id))) {
            const fontSize = Math.max(10.5 / globalScale, 2.5);
            ctx.font = `${fontSize}px var(--font-sans, system-ui, -apple-system, sans-serif)`;
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillStyle = resolvedTheme === 'dark' ? 'rgba(255,255,255,0.92)' : 'rgba(15,23,42,0.92)';
            ctx.globalAlpha = alpha;
            ctx.fillText(label, n.x, n.y + n.val + fontSize + 1);
          }
        }}
        onNodeClick={(node) => {
          setSelectedNode(node as unknown as EntityNode);
        }}
      />
      </div>

      {/* 节点点击侧边检查抽屉 */}
      {selectedNode && (
        <div className="absolute right-4 top-16 z-10 w-72 rounded-xl border border-border bg-card/95 p-4 text-xs shadow-lg backdrop-blur-md animate-in fade-in-0 duration-150">
          <div className="flex items-center justify-between">
            <span className="font-mono text-[10px] uppercase text-primary font-semibold">
              [{selectedNode.type}] 实体图谱详情
            </span>
            <button
              type="button"
              onClick={() => setSelectedNode(null)}
              className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground cursor-pointer"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
          <h4 className="mt-2 text-sm font-bold text-foreground">{selectedNode.name}</h4>
          <div className="mt-3 space-y-1.5 border-t border-border/40 pt-2.5 text-muted-foreground">
            <div className="flex justify-between">
              <span>关联提及次数:</span>
              <span className="font-mono font-medium text-foreground">
                {Math.round(selectedNode.weight ?? 0)} 次
              </span>
            </div>
            <div className="flex justify-between">
              <span>节点 ID:</span>
              <span className="font-mono text-[10px] text-muted-foreground">
                {selectedNode.id}
              </span>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
