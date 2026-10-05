import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router';
import { AlertTriangle } from 'lucide-react';

import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { ApiError } from '@/lib/api/client';
import { spaceService } from '@/lib/api';
import type { ProviderAlert } from '@/lib/api/types.temp';

/** 轮询间隔。provider 挂掉是「这一会儿」的事，半分钟一次足够，也不吵。 */
const POLL_MS = 30_000;

const KIND_LABELS: Record<string, string> = {
  llm: '对话模型',
  embedding: '向量模型',
  rerank: '重排模型',
};

/** 连不上时对用户的实际影响：只报「失败 N 次」的话，用户不知道该不该管 */
const KIND_IMPACT: Record<string, string> = {
  llm: '提问、出题、进化会失败',
  embedding: '检索只能按关键词匹配，找资料变差',
  rerank: '资料按粗排顺序给出，引用可能不够准',
};

function formatWhen(timestamp: number): string {
  if (!timestamp) return '';
  const minutes = Math.round((Date.now() - timestamp) / 60_000);
  if (minutes <= 0) return '刚刚';
  if (minutes < 60) return `${minutes} 分钟前`;
  return `${Math.round(minutes / 60)} 小时前`;
}

/**
 * 顶栏的 provider 告警。
 *
 * provider 连不上时，用户此前能看到的只有一句「回答失败」：不知道是哪个模型挂了，
 * 也不知道该不该去改配置。这里把后端已经记下来的失败如实摆出来，并给一条去设置页的路。
 *
 * 全好的时候整个徽章不出现——常驻一个绿色的「正常」只会变成背景噪音。
 */
export function ProviderAlertBadge() {
  const navigate = useNavigate();
  const [alerts, setAlerts] = useState<ProviderAlert[]>([]);
  const [degraded, setDegraded] = useState(false);
  /** 后端在、但这个端点答不出来（404/5xx）——告警能力本身失效了 */
  const [unavailable, setUnavailable] = useState(false);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const res = await spaceService.getProviderAlerts();
        if (!alive) return;
        setAlerts(res.alerts ?? []);
        setDegraded(Boolean(res.degraded));
        setUnavailable(false);
      } catch (err: unknown) {
        if (!alive) return;
        // 两种失败要分开对待：
        // - 连后端都不通（网络错误，没有 status）：全局请求错误提示已经会说，这里不重复；
        // - 后端在、偏偏这个端点 404/5xx：告警能力自己哑了，而它恰恰是「出事要出声」的
        //   功能——静默吞掉会让人以为一切正常。实测踩过：后端没重启，`/system/alerts`
        //   一直 404，徽章整整一轮没出现，而那时三个 provider 已经全挂了。
        const status = err instanceof ApiError ? err.status : 0;
        setUnavailable(status > 0);
      }
    };
    void load();
    const timer = setInterval(load, POLL_MS);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, []);

  // 告警能力本身失效时给一个克制的提示：不知道有没有模型挂了，比「看起来一切正常」诚实
  if (unavailable) {
    return (
      <span
        className="flex h-8 items-center gap-1.5 rounded-lg border border-border/60 bg-muted/30 px-2.5 text-xs font-medium text-muted-foreground"
        title="取不到模型告警（后端未提供该端点，常见于后端没重启）。此时无法判断模型是否可用。"
      >
        <AlertTriangle className="h-3.5 w-3.5" />
        <span className="hidden sm:inline">告警不可用</span>
      </span>
    );
  }

  if (alerts.length === 0) return null;

  const unresolved = alerts.filter((item) => !item.recovered);
  const tone = degraded
    ? 'border-destructive/40 bg-destructive/10 text-destructive'
    : 'border-accent-warn/40 bg-accent-warn/10 text-accent-warn';

  return (
    <Popover>
      <PopoverTrigger
        className={`flex h-8 items-center gap-1.5 rounded-lg border px-2.5 text-xs font-medium transition-colors cursor-pointer ${tone}`}
        title={degraded ? '有模型连不上' : '有模型刚才出过错，现已恢复'}
      >
        <AlertTriangle className="h-3.5 w-3.5" />
        <span className="hidden sm:inline">
          {degraded ? `${unresolved.length} 个模型连不上` : '模型曾中断'}
        </span>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-80 p-3 text-xs">
        <div className="font-semibold text-foreground">最近半小时的模型调用故障</div>
        <div className="mt-2 space-y-2">
          {alerts.map((item) => (
            <div
              key={`${item.provider_id}-${item.kind}`}
              className="rounded-md border border-border/60 bg-muted/20 px-2.5 py-2"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="font-mono font-medium text-foreground truncate">
                  {item.provider_id}
                </span>
                <span
                  className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium ${
                    item.recovered
                      ? 'bg-accent-insight/10 text-accent-insight'
                      : 'bg-destructive/10 text-destructive'
                  }`}
                >
                  {item.recovered ? '已恢复' : '仍失败'}
                </span>
              </div>
              <div className="mt-1 text-muted-foreground">
                {KIND_LABELS[item.kind] ?? item.kind} · {item.failures}/{item.calls} 次调用失败 ·
                最近 {formatWhen(item.last_failed_at)}
              </div>
              {!item.recovered && KIND_IMPACT[item.kind] && (
                <div className="mt-0.5 text-[11px] text-destructive/90">影响：{KIND_IMPACT[item.kind]}</div>
              )}
            </div>
          ))}
        </div>
        <button
          type="button"
          onClick={() => navigate('/settings')}
          className="mt-2.5 w-full rounded-md border border-border/60 py-1.5 text-[11px] font-medium text-primary hover:bg-muted/40 cursor-pointer"
        >
          去「设置 → 模型」检查配置
        </button>
      </PopoverContent>
    </Popover>
  );
}
