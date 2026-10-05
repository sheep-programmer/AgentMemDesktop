import React from 'react';
import { CheckCircle2 } from 'lucide-react';

export function AboutSection() {
  const components = [
    { name: '后端通信接口 (FastAPI)', status: '正常运行 (8765 端口)', ok: true },
    { name: '关系元数据库 (SQLite 3 WAL)', status: '已连接', ok: true },
    { name: '密集向量存储 (LanceDB v0.17)', status: '就绪', ok: true },
    { name: '深度文档解析器 (Docling Engine)', status: '已就绪', ok: true },
  ];

  return (
    <div className="rounded-2xl border border-border bg-card p-6 space-y-5">
      <div>
        <div className="flex items-center gap-2">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary text-primary-foreground font-bold text-sm">
            AM
          </div>
          <div>
            <h3 className="text-base font-bold text-foreground">AgentMem 本地专家大脑</h3>
            <span className="font-mono text-xs text-muted-foreground">Version 0.1.0-alpha (Phase 1B 地基已就绪)</span>
          </div>
        </div>
        <p className="mt-3 text-xs text-muted-foreground leading-relaxed">
          AgentMem 是一款「本地优先、五层记忆、自我进化」的专家级知识库系统。融合混合检索（Hybrid RRF）、L2 概念图谱抽取与 L3 反思经验自进化闭环，让大模型在垂直领域越用越深，越用越聪明。
        </p>
      </div>

      <div className="border-t border-border/50 pt-4 space-y-2">
        <h4 className="text-xs font-semibold text-foreground mb-2">本地子系统运行健康状态</h4>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-xs">
          {components.map((comp) => (
            <div key={comp.name} className="flex items-center justify-between rounded-lg border border-border/60 bg-background/60 p-2.5">
              <span className="text-foreground">{comp.name}</span>
              <span className="flex items-center gap-1 font-mono text-[11px] text-accent-insight">
                <CheckCircle2 className="h-3.5 w-3.5" />
                {comp.status}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
