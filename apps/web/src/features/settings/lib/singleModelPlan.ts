import type { ModelRole, RoleBindings } from '@/lib/api/types.temp';

/** 角色的中文名，删除服务商、一键改绑时说清楚动的是谁 */
export const ROLE_NAMES: Record<ModelRole, string> = {
  chat: '主对话',
  fast: '快速改写',
  distill: '经验总结',
  judge: '评测打分',
  embedding: '向量嵌入',
  rerank: '重排',
};

/**
 * 能交给同一个大模型的四个角色。
 *
 * embedding / rerank 不在其中：它们要的是专门的向量模型和重排模型，普通对话模型的接口
 * 根本不提供这两种能力，绑上去只会让检索全部失败。
 */
export const LLM_ROLES = ['chat', 'fast', 'distill', 'judge'] as const satisfies readonly ModelRole[];

export type LlmRole = (typeof LLM_ROLES)[number];

/** 一处要改的绑定：从哪个服务商改到哪个（`from` 为 null 表示原先没绑） */
export interface BindingChange {
  role: LlmRole;
  from: string | null;
  to: string;
}

/**
 * 算出「把四个 LLM 角色都交给 providerId」要改哪些角色。
 *
 * 已经绑在它身上的角色不列出来，这样确认框里只出现真正会变的；返回空数组就说明
 * 四个角色已经都是它，按钮应当置灰。
 */
export function planSingleModel(bindings: RoleBindings, providerId: string): BindingChange[] {
  return LLM_ROLES.filter((role) => bindings[role] !== providerId).map((role) => ({
    role,
    from: bindings[role] ?? null,
    to: providerId,
  }));
}

/** 把改动合并进现有绑定，得到一次提交用的完整绑定（接口是整体覆盖，不是局部更新） */
export function applyChanges(bindings: RoleBindings, changes: BindingChange[]): RoleBindings {
  const next: RoleBindings = { ...bindings };
  for (const change of changes) next[change.role] = change.to;
  return next;
}

/**
 * 四个 LLM 角色若已统一绑在同一个服务商上，返回它的 id；否则返回 null。
 * 用来给下拉框选默认值，也用来判断简易区域要不要默认展开。
 */
export function sharedLlmProvider(bindings: RoleBindings): string | null {
  const first = bindings[LLM_ROLES[0]];
  if (!first) return null;
  return LLM_ROLES.every((role) => bindings[role] === first) ? first : null;
}
