import { apiClient } from '../client';
import type {
  ProviderItem,
  ProviderPublic,
  ProviderCreate,
  ProviderUpdate,
  ProviderHealth,
  DiscoverResponse,
  RoleBindings,
  RolesUpdateResponse,
  LocalAgentScanResponse,
  UsageResponse,
} from '../types';
import { mockProviders, mockRoleBindings, mockLocalAgentScanResponse } from '../mock/data';
import { isMockMode } from './spaces';

export const providerService = {
  async getProviders(): Promise<ProviderItem[]> {
    if (isMockMode()) return mockProviders;
    const list = await apiClient<ProviderPublic[]>('/providers');
    return list.map((p) => ({
      ...p,
      name: p.id,
      status: p.enabled ? 'online' : 'offline',
    }));
  },

  async getProvider(id: string): Promise<ProviderPublic> {
    if (isMockMode()) {
      return mockProviders.find((p) => p.id === id) || mockProviders[0];
    }
    return apiClient<ProviderPublic>(`/providers/${id}`);
  },

  async createProvider(data: ProviderCreate): Promise<ProviderPublic> {
    if (isMockMode()) {
      const created: ProviderItem = {
        id: data.id,
        kind: data.kind,
        adapter: data.adapter,
        model: data.model,
        base_url: data.base_url || null,
        has_api_key: Boolean(data.api_key),
        api_key_hint: data.api_key ? '••••' + data.api_key.slice(-4) : null,
        api_key_from_env: null,
        dimension: data.dimension || null,
        device: data.device || null,
        enabled: data.enabled ?? true,
        extra: data.extra || {},
        name: data.id,
        status: 'online',
        latency_ms: 120,
      };
      mockProviders.push(created);
      return created;
    }
    return apiClient<ProviderPublic>('/providers', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  },

  async updateProvider(id: string, data: ProviderUpdate): Promise<ProviderPublic> {
    if (isMockMode()) {
      const found = mockProviders.find((p) => p.id === id);
      if (found) {
        if (data.model !== undefined && data.model !== null) found.model = data.model;
        if (data.base_url !== undefined) found.base_url = data.base_url;
        if (data.enabled !== undefined && data.enabled !== null) found.enabled = data.enabled;
        if (data.kind !== undefined && data.kind !== null) found.kind = data.kind;
        if (data.adapter !== undefined && data.adapter !== null) found.adapter = data.adapter;
        if (data.dimension !== undefined) found.dimension = data.dimension;
        if (data.device !== undefined) found.device = data.device;
        if (data.api_key) {
          found.has_api_key = true;
          found.api_key_hint = '••••' + data.api_key.slice(-4);
          found.api_key_from_env = null;
        }
        return found;
      }
      return mockProviders[0];
    }
    return apiClient<ProviderPublic>(`/providers/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    });
  },

  async deleteProvider(id: string): Promise<{ deleted: boolean }> {
    if (isMockMode()) {
      const idx = mockProviders.findIndex((p) => p.id === id);
      if (idx >= 0) mockProviders.splice(idx, 1);
      return { deleted: true };
    }
    return apiClient<{ deleted: boolean }>(`/providers/${id}`, {
      method: 'DELETE',
    });
  },

  async checkHealth(id: string): Promise<ProviderHealth> {
    if (isMockMode()) {
      return {
        ok: true,
        latency_ms: Math.floor(Math.random() * 80) + 40,
        resolved_model: 'mock-model',
      };
    }
    return apiClient<ProviderHealth>(`/providers/${id}/health`, {
      method: 'POST',
    });
  },

  async discoverProviders(
    data: { adapter: string; base_url?: string | null; api_key?: string | null } = {
      adapter: 'ollama_native',
      base_url: 'http://localhost:11434',
    },
  ): Promise<DiscoverResponse> {
    if (isMockMode()) {
      return {
        adapter: data.adapter || 'ollama_native',
        base_url: data.base_url || 'http://localhost:11434',
        models: [
          {
            id: 'llama3:latest',
            family: 'llama',
          },
          {
            id: 'nomic-embed-text:latest',
            family: 'nomic',
          },
        ],
      };
    }
    return apiClient<DiscoverResponse>('/providers/discover', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  },

  async scanLocalAgents(): Promise<LocalAgentScanResponse> {
    if (isMockMode()) {
      return mockLocalAgentScanResponse;
    }
    return apiClient<LocalAgentScanResponse>('/providers/local-agents');
  },

  async importLocalAgents(suggestedIds: string[]): Promise<ProviderPublic[]> {
    if (isMockMode()) {
      const candidates = mockLocalAgentScanResponse.candidates || [];
      const matched = candidates.filter((c) => suggestedIds.includes(c.suggested_id));
      const createdList: ProviderPublic[] = [];
      for (const c of matched) {
        c.already_imported = true;
        const prov: ProviderItem = {
          id: c.suggested_id,
          kind: c.kind,
          adapter: c.adapter,
          model: c.model || null,
          base_url: c.base_url || null,
          has_api_key: c.has_api_key,
          api_key_hint: c.api_key_hint || null,
          api_key_from_env: c.api_key_env || null,
          enabled: true,
          status: 'online',
          name: c.suggested_id,
          latency_ms: 35,
        };
        mockProviders.push(prov);
        createdList.push(prov);
      }
      return createdList;
    }
    return apiClient<ProviderPublic[]>('/providers/import-local', {
      method: 'POST',
      body: JSON.stringify({ suggested_ids: suggestedIds }),
    });
  },

  async getRoleBindings(): Promise<RoleBindings> {
    if (isMockMode()) return mockRoleBindings;
    return apiClient<RoleBindings>('/providers/roles');
  },

  async updateRoleBindings(roles: RoleBindings): Promise<RolesUpdateResponse> {
    if (isMockMode()) {
      Object.assign(mockRoleBindings, roles);
      return {
        roles: mockRoleBindings,
        requires_reindex: false,
      };
    }
    return apiClient<RolesUpdateResponse>('/providers/roles', {
      method: 'PUT',
      body: JSON.stringify({ roles }),
    });
  },

  async getUsage(params: {
    group_by?: 'provider' | 'model' | 'purpose' | 'kind';
    since?: number | null;
    /**
     * 限定能力类型。看缓存命中率时**必须**传 `'llm'`：
     * embedding 与 rerank 没有前缀缓存这回事，把它们算进分母只会把命中率
     * 稀释成一个没有意义的数（实测 embedding 能占到输入 token 的四成）。
     */
    kind?: 'llm' | 'embedding' | 'rerank' | null;
  } = {}): Promise<UsageResponse> {
    const groupBy = params.group_by || 'provider';
    if (isMockMode()) {
      return {
        group_by: groupBy,
        items: [],
        total_tokens: 0,
      };
    }
    const searchParams = new URLSearchParams();
    searchParams.set('group_by', groupBy);
    if (params.since != null) {
      searchParams.set('since', String(params.since));
    }
    if (params.kind != null) {
      searchParams.set('kind', params.kind);
    }
    return apiClient<UsageResponse>(`/providers/usage?${searchParams.toString()}`);
  },
};
