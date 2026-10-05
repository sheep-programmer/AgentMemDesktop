import { apiClient, isMockMode } from "../client";
import { fetchSSE } from "../sse";
export { isMockMode };
import type {
  ProviderAlerts,
  Space,
  SpaceCreate,
  SpaceUpdate,
  SystemCapabilities,
  SystemStats,
} from '../types';
import { mockSpaces } from '../mock/data';


export const spaceService = {
  async getSpaces(): Promise<Space[]> {
    if (isMockMode()) return mockSpaces;
    return apiClient<Space[]>('/spaces');
  },

  async getSpace(id: string): Promise<Space> {
    if (isMockMode()) {
      const found = mockSpaces.find((s) => s.id === id);
      if (found) return found;
      return mockSpaces[0];
    }
    return apiClient<Space>(`/spaces/${id}`);
  },

  async createSpace(data: SpaceCreate): Promise<Space> {
    if (isMockMode()) {
      const newSpace: Space = {
        id: `space-${Date.now()}`,
        name: data.name,
        domain: data.domain,
        description: data.description || null,
        icon: data.icon || null,
        color: data.color || null,
        doc_count: 0,
        insight_count: 0,
        expertise_overall: null,
        created_at: Date.now(),
        updated_at: Date.now(),
      };
      mockSpaces.push(newSpace);
      return newSpace;
    }
    return apiClient<Space>('/spaces', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  },

  async updateSpace(id: string, data: SpaceUpdate): Promise<Space> {
    if (isMockMode()) {
      const found = mockSpaces.find((s) => s.id === id);
      if (found) {
        Object.assign(found, data, { updated_at: Date.now() });
        return found;
      }
      return mockSpaces[0];
    }
    return apiClient<Space>(`/spaces/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    });
  },

  async deleteSpace(id: string): Promise<{ deleted: boolean }> {
    if (isMockMode()) {
      const idx = mockSpaces.findIndex((s) => s.id === id);
      if (idx >= 0) mockSpaces.splice(idx, 1);
      return { deleted: true };
    }
    return apiClient<{ deleted: boolean }>(`/spaces/${id}`, {
      method: 'DELETE',
    });
  },

  /**
   * 重建一个空间的切片与向量索引（SSE）。换了向量模型之后必须跑：旧向量与新模型不兼容，
   * 维度不同时新文档的向量化会直接失败。
   */
  async reindexSpace(
    spaceId: string,
    handlers: {
      onBegin?: (data: { total: number }) => void;
      onProgress?: (data: { done: number; total: number }) => void;
      onError?: (data: { document_id?: string; message: string }) => void;
      onDone?: (data: { total: number; reindexed: number }) => void;
    },
  ): Promise<void> {
    if (isMockMode()) {
      handlers.onBegin?.({ total: 0 });
      handlers.onDone?.({ total: 0, reindexed: 0 });
      return;
    }
    await fetchSSE({
      url: `/spaces/${spaceId}/reindex`,
      method: 'POST',
      handlers: {
        begin: (d) => handlers.onBegin?.(d as { total: number }),
        progress: (d) => handlers.onProgress?.(d as { done: number; total: number }),
        error: (d) => handlers.onError?.(d as { document_id?: string; message: string }),
        done: (d) => handlers.onDone?.(d as { total: number; reindexed: number }),
      },
    });
  },

  /** 导出一个 Space 的完整备份包。走原生 fetch：apiClient 只解析 JSON，拿不到二进制。 */
  async exportSpace(spaceId: string): Promise<Blob> {
    if (isMockMode()) return new Blob(['mock'], { type: 'application/zip' });
    const res = await fetch(`/api/v1/spaces/${spaceId}/export`);
    if (!res.ok) throw new Error(`导出失败（HTTP ${res.status}）`);
    return res.blob();
  },

  /** 从备份包还原一个 Space。multipart 上传，不能带 JSON 的 Content-Type。 */
  async importSpace(file: File): Promise<{ space_id: string }> {
    if (isMockMode()) return { space_id: mockSpaces[0]?.id ?? 'mock-space' };
    const form = new FormData();
    form.append('file', file);
    return apiClient<{ space_id: string }>('/spaces/import', {
      method: 'POST',
      body: form,
    });
  },

  async getSystemCapabilities(): Promise<SystemCapabilities> {
    if (isMockMode()) {
      return {
        rerank_available: true,
        local_embedding: true,
        docling_available: false,
        graph_enabled: true,
        chat_available: true,
        distill_available: true,
      };
    }
    return apiClient<SystemCapabilities>('/capabilities');
  },

  /** 最近半小时里失败过的 provider；界面据此提示「某个模型连不上」。 */
  async getProviderAlerts(): Promise<ProviderAlerts> {
    if (isMockMode()) {
      return { window_ms: 1_800_000, alerts: [], degraded: false };
    }
    return apiClient<ProviderAlerts>('/system/alerts');
  },

  async getSystemStats(): Promise<SystemStats> {
    if (isMockMode()) {
      return {
        space_count: mockSpaces.length,
        document_count: 12,
        chunk_count: 86,
        insight_count: 14,
        disk_usage_bytes: 45200000,
      };
    }
    return apiClient<SystemStats>('/stats');
  },
};
