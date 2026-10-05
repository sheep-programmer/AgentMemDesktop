import { useState, useEffect } from 'react';
import { Laptop, Bot, KeyRound } from 'lucide-react';
import { toast } from 'sonner';
import type { ProviderItem, LocalAgentCandidate } from '@/lib/api/types.temp';
import { providerService } from '@/lib/api';
import type { CandidateGroup } from '../components/LocalScanGroupList';

interface UseLocalScanOptions {
  isOpen: boolean;
  existingProviders: ProviderItem[];
  onImport: (providers: ProviderItem[]) => void;
  onClose: () => void;
}

export function useLocalScan({ isOpen, existingProviders, onImport, onClose }: UseLocalScanOptions) {
  const [isScanning, setIsScanning] = useState(false);
  const [hasScanned, setHasScanned] = useState(false);
  const [candidates, setCandidates] = useState<LocalAgentCandidate[]>([]);
  const [scannedPaths, setScannedPaths] = useState<string[]>([]);
  const [errors, setErrors] = useState<string[]>([]);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [isImporting, setIsImporting] = useState(false);

  const handleScan = async () => {
    setIsScanning(true);
    try {
      const [discoverResult, agentResult] = await Promise.allSettled([
        providerService.discoverProviders(),
        providerService.scanLocalAgents(),
      ]);

      // 两路扫描都失败时要说清楚是「扫描失败」，不能让它表现成「本机没有模型」——
      // 后者会让用户以为自己没装 Ollama，转头去装一个已经装好的东西
      if (discoverResult.status === 'rejected' && agentResult.status === 'rejected') {
        toast.error('扫描本机失败，请确认后端在运行');
        return;
      }

      const existingIds = new Set(existingProviders.map((p) => p.id));
      const localServices: LocalAgentCandidate[] = [];

      if (discoverResult.status === 'fulfilled') {
        const res = discoverResult.value;
        for (const m of res.models || []) {
          const isLMStudio = res.adapter === 'lm_studio' || res.base_url?.includes('1234');
          localServices.push({
            source: isLMStudio ? 'lm_studio' : 'ollama',
            source_label: isLMStudio ? 'LM Studio · 本地服务' : 'Ollama · 本地服务',
            suggested_id: m.id,
            kind: m.id.toLowerCase().includes('embed') ? 'embedding' : 'llm',
            adapter: res.adapter || 'ollama_native',
            model: m.id,
            base_url: res.base_url || 'http://localhost:11434',
            has_api_key: false,
            already_imported: existingIds.has(m.id),
            note: '本机运行中 · 免密钥直连',
          });
        }
      }

      let agentCandidates: LocalAgentCandidate[] = [];
      let scanned: string[] = [];
      let errs: string[] = [];
      if (agentResult.status === 'fulfilled') {
        agentCandidates = (agentResult.value.candidates || []).map((c) => ({
          ...c,
          already_imported: c.already_imported || existingIds.has(c.suggested_id),
        }));
        scanned = agentResult.value.scanned_paths || [];
        errs = agentResult.value.errors || [];
      }

      const all = [...localServices, ...agentCandidates];
      setCandidates(all);
      setScannedPaths(scanned);
      setErrors(errs);
      setHasScanned(true);

      const available = all.filter((c) => !c.already_imported).map((c) => c.suggested_id);
      setSelectedIds(new Set(available));

      if (all.length > 0) toast.success(`扫描完成，发现 ${all.length} 个本地配置与模型实例`);
    } catch (err: unknown) {
      toast.error(`扫描失败: ${(err as Error)?.message || '未知错误'}`);
    } finally {
      setIsScanning(false);
    }
  };

  useEffect(() => {
    if (isOpen && !hasScanned) handleScan();
  }, [isOpen]);

  const handleToggle = (id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const handleToggleGroup = (ids: string[], select: boolean) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      ids.forEach((id) => (select ? next.add(id) : next.delete(id)));
      return next;
    });
  };

  const handleConfirmImport = async () => {
    const selected = candidates.filter((c) => selectedIds.has(c.suggested_id) && !c.already_imported);
    if (selected.length === 0) return;

    setIsImporting(true);
    const imported: ProviderItem[] = [];
    let failedCount = 0;

    try {
      const localItems = selected.filter((c) => c.source === 'ollama' || c.source === 'lm_studio');
      for (const item of localItems) {
        try {
          const created = await providerService.createProvider({
            id: item.suggested_id,
            kind: item.kind,
            adapter: item.adapter,
            model: item.model,
            base_url: item.base_url,
            enabled: true,
          });
          imported.push({ ...created, name: created.id, status: 'online', latency_ms: 15 });
        } catch {
          // 逐条失败也要记账：只报成功数、对失败只字不提，用户会以为选中的都进来了
          failedCount += 1;
        }
      }

      const agentItems = selected.filter((c) => c.source !== 'ollama' && c.source !== 'lm_studio');
      if (agentItems.length > 0) {
        const ids = agentItems.map((c) => c.suggested_id);
        const importedAgents = await providerService.importLocalAgents(ids);
        importedAgents.forEach((p) => {
          imported.push({ ...p, name: p.id, status: p.enabled ? 'online' : 'offline', latency_ms: 20 });
        });
      }

      if (imported.length > 0) {
        onImport(imported);
        if (failedCount > 0) {
          toast.warning(`已导入 ${imported.length} 个，${failedCount} 个失败`);
        } else {
          toast.success(`已成功导入 ${imported.length} 个模型服务商`);
        }
        onClose();
      } else {
        // 一个都没成功时原本**毫无反应**：不弹提示、弹窗不关、也不解释，
        // 用户只能看着按钮转完一圈然后什么都没发生
        toast.error(
          failedCount > 0 ? `${failedCount} 个模型服务商导入失败` : '没有可导入的模型服务商',
        );
      }
    } catch (err: unknown) {
      toast.error(`导入失败: ${(err as Error)?.message || '未知错误'}`);
    } finally {
      setIsImporting(false);
    }
  };

  const groups: CandidateGroup[] = [
    {
      id: 'local_services',
      title: '本机运行中的模型服务',
      icon: Laptop,
      candidates: candidates.filter((c) => c.source === 'ollama' || c.source === 'lm_studio'),
    },
    {
      id: 'agent_configs',
      title: '已有 Agent 配置',
      icon: Bot,
      candidates: candidates.filter((c) => ['claude_code', 'codex', 'continue'].includes(c.source)),
    },
    {
      id: 'env_configs',
      title: '环境变量',
      icon: KeyRound,
      candidates: candidates.filter((c) => c.source === 'environment'),
    },
  ];

  const allImported = candidates.length > 0 && candidates.every((c) => c.already_imported);

  return {
    isScanning,
    hasScanned,
    candidates,
    scannedPaths,
    errors,
    selectedIds,
    isImporting,
    groups,
    allImported,
    handleScan,
    handleToggle,
    handleToggleGroup,
    handleConfirmImport,
  };
}
