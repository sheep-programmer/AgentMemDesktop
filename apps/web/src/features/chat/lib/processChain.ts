import type { ProcessStage } from '@/lib/api/types.temp';

/** 四个阶段都没跑过——链条此时是「将要发生什么」的预告，不能说成已完成。 */
export function isChainNotStarted(stages: ProcessStage[]): boolean {
  return (
    stages.length === 0 || stages.every((stage) => stage.status === 'pending')
  );
}

/** 一条都拿不到阶段时的占位链条。 */
export const CHAIN_PLACEHOLDER =
  '⟳ 意图解析 → 🔍 知识检索 → 🧩 经验注入 → ✍️ 推理生成';

/** Show the user's current state first; technical stages remain in the details. */
export function buildProcessSummary(
  stages: ProcessStage[],
  isStreaming: boolean,
): string {
  const failed = stages.find((stage) => stage.status === 'failed');
  if (failed)
    return failed.detail?.includes('用户主动中止')
      ? '回答已停止'
      : '回答未完成';
  if (isStreaming) {
    const active = stages.find((stage) => stage.status === 'running');
    const labels: Record<string, string> = {
      rewrite: '正在理解问题',
      retrieval: '正在查找相关资料',
      insights: '正在参考已有经验',
      generating: '正在撰写回答',
    };
    return labels[active?.id ?? ''] || '正在准备回答';
  }
  if (isChainNotStarted(stages)) return '准备好开始提问';
  const complete = stages.some(
    (stage) => stage.id === 'generating' && stage.status === 'done',
  );
  if (!complete) return '查看本次回答进度';
  const count = stages
    .find((stage) => stage.id === 'retrieval' && stage.status === 'done')
    ?.detail?.match(/(?:召回|检索到)\s*(\d+)\s*条/)?.[1];
  const parts = ['回答已完成'];
  if (count) parts.push(`找到 ${count} 条资料`);
  if (stages.some((stage) => stage.warning)) parts.push('检索受限');
  return parts.join(' · ');
}

/**
 * 构造过程条的链式摘要行。
 *
 * 两条规矩：
 * 1. 每一环的措辞只由**该环自己的 status** 决定。拿整体的 `isStreaming` 去描述单个阶段，
 *    会让还没跑到的阶段谎称跑完了（新会话一进来就写着「推理生成完成」就是这么来的）。
 * 2. 条数只从真实的 `detail` 里抠，取不到就不提条数，不编一个看着像真的数字。
 */
export function buildSummaryChain(
  stages: ProcessStage[],
  isStreaming: boolean,
): string {
  const parts: string[] = [];

  stages.forEach((stage) => {
    const count = stage.detail?.match(/(\d+)/)?.[1];
    const { status } = stage;

    if (stage.id === 'rewrite') {
      parts.push(
        status === 'done'
          ? '⟳ 改写查询'
          : status === 'running'
            ? '⟳ 意图改写中'
            : '⟳ 意图解析',
      );
    } else if (stage.id === 'retrieval') {
      const base =
        status === 'done'
          ? count
            ? `🔍 检索到 ${count} 条资料`
            : '🔍 检索完成'
          : status === 'running'
            ? '🔍 检索文献中'
            : '🔍 知识检索';
      // 检索降级要进摘要行：回答生成完过程条会自动折叠，只放在展开详情里等于没说
      parts.push(stage.warning ? `${base}（⚠️ 检索降级）` : base);
    } else if (stage.id === 'insights') {
      parts.push(
        status === 'done'
          ? count
            ? `🧩 应用 ${count} 条经验`
            : '🧩 经验匹配完成'
          : status === 'running'
            ? '🧩 匹配实战经验'
            : '🧩 经验注入',
      );
    } else if (stage.id === 'generating') {
      parts.push(
        status === 'done'
          ? '✍️ 推理生成完成'
          : status === 'running' || isStreaming
            ? '✍️ 正在生成深度解答'
            : '✍️ 推理生成',
      );
    }
  });

  return parts.length > 0 ? parts.join(' → ') : CHAIN_PLACEHOLDER;
}
