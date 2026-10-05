import type { ProviderHealth, RoleBindings } from '@/lib/api/types.temp';

/** 单个服务商的检测状态：检测中 / 在线（带延迟）/ 失败（带原因） */
export type ProviderCheckState =
  | { status: 'checking' }
  | { status: 'online'; latencyMs: number | null }
  | { status: 'failed'; error: string };

/**
 * 取出被角色绑定的服务商 id，去重、并且只留下服务商列表里真实存在的。
 *
 * 多个角色常常绑同一个服务商（chat / distill / judge 共用一个大模型很普遍），
 * 不去重就会对同一个服务发好几次测试请求——对按量计费的 API 来说是白花钱。
 * 绑定指向已删除的服务商时后端会 404，没有意义，卡片上已经显示「未绑定」。
 */
export function boundProviderIds(bindings: RoleBindings, knownIds: Iterable<string>): string[] {
  const known = new Set(knownIds);
  const ids = new Set<string>();
  for (const providerId of Object.values(bindings)) {
    if (providerId && known.has(providerId)) ids.add(providerId);
  }
  return [...ids];
}

/** 把一次实测结果（或请求本身抛出的异常）归一成卡片要显示的状态 */
export function toCheckState(outcome: PromiseSettledResult<ProviderHealth>): ProviderCheckState {
  if (outcome.status === 'rejected') {
    const reason = outcome.reason as { message?: unknown } | undefined;
    const message = typeof reason?.message === 'string' && reason.message ? reason.message : '请求失败';
    return { status: 'failed', error: message };
  }
  const health = outcome.value;
  if (health.ok) return { status: 'online', latencyMs: health.latency_ms ?? null };
  return { status: 'failed', error: health.error || '无法建立连接' };
}

/**
 * 并行检测一组服务商，每个出结果就回调一次。
 *
 * 逐个回调而不是等全部结束再一起刷新：本地模型第一次检测要加载权重，可能要十几秒，
 * 云端接口一般几百毫秒就回来了，没必要让快的陪着慢的一起转圈。
 */
export async function checkProviders(
  ids: string[],
  check: (id: string) => Promise<ProviderHealth>,
  onResult: (id: string, state: ProviderCheckState) => void,
): Promise<void> {
  await Promise.all(
    ids.map(async (id) => {
      const [outcome] = await Promise.allSettled([check(id)]);
      onResult(id, toCheckState(outcome));
    }),
  );
}
