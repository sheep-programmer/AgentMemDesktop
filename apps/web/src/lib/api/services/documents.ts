import { apiClient } from '../client';
import { fetchSSE } from '../sse';
import type {
  DocumentItem,
  DocumentContent,
  ChunkItem,
  PageResponse,
  DocumentRetryBeginEvent,
  DocumentRetryProgressEvent,
  DocumentRetryErrorEvent,
  DocumentRetryDoneEvent,
} from '../types';
import { mockPage } from '../mock/page';
import { mockDocuments, mockDocumentBundle } from '../mock/data';
import { isMockMode } from './spaces';
import { useSpaceStore } from '@/stores/useSpaceStore';

export type { PageResponse };

export interface UploadRejection {
  filename: string;
  code: string;
  message: string;
}

export interface UploadResult {
  count: number;
  documents: DocumentItem[];
  /** 没收下的文件及原因（重复、为空、超过大小上限）；其余文件照常处理 */
  rejected: UploadRejection[];
}

export interface DocumentListOptions {
  signal?: AbortSignal;
  limit?: number;
  cursor?: string | null;
  status?: string;
  q?: string;
  tag?: string;
}

export interface DocumentRetryHandlers {
  onBegin?: (data: DocumentRetryBeginEvent) => void;
  onProgress?: (data: DocumentRetryProgressEvent) => void;
  onError?: (data: DocumentRetryErrorEvent) => void;
  onDone?: (data: DocumentRetryDoneEvent) => void;
}

const resolveSpaceId = (spaceId?: string) =>
  spaceId || useSpaceStore.getState().currentSpaceId;

export const documentService = {
  async getDocuments(
    spaceId: string,
    options?: DocumentListOptions | number,
    cursorParam?: string | null,
  ): Promise<PageResponse<DocumentItem>> {
    let limit = 50;
    let cursor: string | null = null;
    let status: string | undefined;
    let q: string | undefined;
    let tag: string | undefined;
    let signal: AbortSignal | undefined;

    if (typeof options === 'number') {
      // 兼容旧调用签名 (spaceId, page, pageSize) 或 (spaceId, limit, cursor)
      limit = typeof cursorParam === 'number' ? cursorParam : options || 50;
      cursor = typeof cursorParam === 'string' ? cursorParam : null;
    } else if (options) {
      if (options.limit !== undefined) limit = options.limit;
      if (options.cursor !== undefined) cursor = options.cursor;
      status = options.status;
      q = options.q;
      tag = options.tag;
      signal = options.signal;
    }

    if (isMockMode()) {
      let filtered = mockDocuments.filter(
        (d) => d.space_id === spaceId || !spaceId,
      );
      if (status) filtered = filtered.filter((d) => d.status === status);
      if (q)
        filtered = filtered.filter(
          (d) =>
            d.title.toLowerCase().includes(q.toLowerCase()) ||
            d.source_uri?.toLowerCase().includes(q.toLowerCase()),
        );
      return mockPage(
        [...filtered].sort(
          (a, b) => b.created_at - a.created_at || b.id.localeCompare(a.id),
        ),
        limit,
        cursor,
      );
    }

    const params = new URLSearchParams();
    if (limit) params.set('limit', String(limit));
    if (cursor) params.set('cursor', cursor);
    if (status) params.set('status', status);
    if (q) params.set('q', q);
    if (tag) params.set('tag', tag);
    const qs = params.toString();

    const res = await apiClient<PageResponse<DocumentItem>>(
      `/spaces/${spaceId}/documents${qs ? `?${qs}` : ''}`,
      { signal },
    );
    return {
      items: res.items || [],
      total: res.total ?? 0,
      next_cursor: res.next_cursor ?? null,
    };
  },

  async uploadFiles(spaceId: string, files: File[]): Promise<UploadResult> {
    if (isMockMode()) {
      const createdDocs: DocumentItem[] = files.map((file, i) => ({
        id: `doc-${Date.now()}-${i}`,
        space_id: spaceId,
        title: file.name,
        source_type: 'file',
        source_uri: file.name,
        source_url: file.name,
        sha256: `sha-${Math.random().toString(36).slice(2)}`,
        size_bytes: file.size,
        mime: file.type || 'application/octet-stream',
        status: 'parsing',
        token_count: 0,
        meta: {},
        created_at: Date.now(),
        updated_at: Date.now(),
      }));
      mockDocuments.unshift(...createdDocs);
      return {
        count: createdDocs.length,
        documents: createdDocs,
        rejected: [],
      };
    }

    const formData = new FormData();
    for (const file of files) {
      formData.append('files', file);
    }

    const res = await apiClient<{
      documents: DocumentItem[];
      rejected?: UploadRejection[];
    }>(`/spaces/${spaceId}/documents/upload`, {
      method: 'POST',
      body: formData,
    });
    // 后端从不返回 count：此前读 res.count 落空，提示里写的永远是「拖进来几个」而不是「收下几个」
    return {
      count: res.documents.length,
      documents: res.documents,
      rejected: res.rejected ?? [],
    };
  },

  async pasteText(
    spaceId: string,
    title: string,
    content: string,
  ): Promise<DocumentItem> {
    if (isMockMode()) {
      const doc: DocumentItem = {
        id: `doc-paste-${Date.now()}`,
        space_id: spaceId,
        title,
        source_type: 'paste',
        source_uri: 'pasted_text',
        source_url: 'pasted_text',
        sha256: `sha-${Math.random().toString(36).slice(2)}`,
        size_bytes: content.length,
        status: 'chunking',
        token_count: Math.ceil(content.length / 4),
        meta: {},
        created_at: Date.now(),
        updated_at: Date.now(),
      };
      mockDocuments.unshift(doc);
      return doc;
    }

    return apiClient<DocumentItem>(`/spaces/${spaceId}/documents/paste`, {
      method: 'POST',
      body: JSON.stringify({ title, content }),
    });
  },

  async ingestUrl(
    spaceId: string,
    url: string,
    title?: string,
  ): Promise<DocumentItem> {
    if (isMockMode()) {
      const doc: DocumentItem = {
        id: `doc-url-${Date.now()}`,
        space_id: spaceId,
        title: title || url,
        source_type: 'url',
        source_uri: url,
        source_url: url,
        sha256: `sha-${Math.random().toString(36).slice(2)}`,
        size_bytes: 1024,
        mime: 'text/html',
        status: 'pending',
        token_count: 0,
        meta: {},
        created_at: Date.now(),
        updated_at: Date.now(),
      };
      mockDocuments.unshift(doc);
      return doc;
    }

    return apiClient<DocumentItem>(`/spaces/${spaceId}/documents/url`, {
      method: 'POST',
      body: JSON.stringify({ url, title }),
    });
  },

  async getDocument(
    documentId: string,
    spaceId?: string,
  ): Promise<DocumentItem> {
    const space = resolveSpaceId(spaceId);
    if (isMockMode()) {
      const found = mockDocuments.find((d) => d.id === documentId);
      if (found) return found;
      return mockDocuments[0];
    }
    return apiClient<DocumentItem>(`/spaces/${space}/documents/${documentId}`);
  },

  async getDocumentContent(
    documentId: string,
    spaceId?: string,
  ): Promise<DocumentContent> {
    const space = resolveSpaceId(spaceId);
    if (isMockMode()) {
      const bundle = mockDocumentBundle(documentId);
      return {
        document_id: documentId,
        title:
          mockDocuments.find((doc) => doc.id === documentId)?.title ??
          '示例文档',
        markdown: bundle.markdown,
      };
    }
    return apiClient<DocumentContent>(
      `/spaces/${space}/documents/${documentId}/content`,
    );
  },

  /**
   * 原始文件的地址（给 pdf.js 直接去取，走 Range 分段下载，不经过 apiClient）。
   *
   * 示例模式没有后端，返回 null，由调用方提示「原始文件不可用」。
   */
  getDocumentRawUrl(documentId: string, spaceId?: string): string | null {
    if (isMockMode()) return null;
    const space = resolveSpaceId(spaceId);
    return `/api/v1/spaces/${encodeURIComponent(space)}/documents/${encodeURIComponent(documentId)}/raw`;
  },

  /**
   * 取文档的单页切片列表。
   * 后端单页 limit 上限为 500，cursor 值为切片 ordinal。
   */
  async getDocumentChunks(
    documentId: string,
    limit = 500,
    cursor?: string | null,
    spaceId?: string,
  ): Promise<PageResponse<ChunkItem>> {
    const space = resolveSpaceId(spaceId);
    if (isMockMode()) {
      // 与正文同源：偏移由 mockDocumentBundle 现算，阅读器的高亮与定位才对得上
      const items = mockDocumentBundle(documentId).chunks;
      return { items, total: items.length, next_cursor: null };
    }
    const params = new URLSearchParams();
    if (limit) params.set('limit', String(limit));
    if (cursor) params.set('cursor', cursor);
    const qs = params.toString();

    const res = await apiClient<PageResponse<ChunkItem>>(
      `/spaces/${space}/documents/${documentId}/chunks${qs ? `?${qs}` : ''}`,
    );
    return {
      items: res.items || [],
      total: res.total ?? 0,
      next_cursor: res.next_cursor ?? null,
    };
  },

  /**
   * 循环翻页拉全切片列表，直到 next_cursor 为空。
   * 单页 limit 设置为后端上限 500，通过 onProgress 持续通知当前拉取条数与总数。
   */
  async getAllDocumentChunks(
    documentId: string,
    spaceId?: string,
    onProgress?: (loaded: number, total: number) => void,
  ): Promise<ChunkItem[]> {
    const allChunks: ChunkItem[] = [];
    let cursor: string | null = null;
    let hasMore = true;
    const limit = 500;

    while (hasMore) {
      const res = await this.getDocumentChunks(
        documentId,
        limit,
        cursor,
        spaceId,
      );
      const items = res.items || [];
      allChunks.push(...items);
      const total = res.total || allChunks.length;
      onProgress?.(allChunks.length, total);

      if (res.next_cursor && items.length > 0) {
        cursor = res.next_cursor;
      } else {
        hasMore = false;
      }
    }
    return allChunks;
  },

  async deleteDocument(
    documentId: string,
    spaceId?: string,
  ): Promise<{ deleted: boolean }> {
    const space = resolveSpaceId(spaceId);
    if (isMockMode()) {
      const idx = mockDocuments.findIndex((d) => d.id === documentId);
      if (idx >= 0) mockDocuments.splice(idx, 1);
      return { deleted: true };
    }
    return apiClient<{ deleted: boolean }>(
      `/spaces/${space}/documents/${documentId}`,
      {
        method: 'DELETE',
      },
    );
  },

  async reprocessDocument(
    documentId: string,
    spaceId?: string,
  ): Promise<DocumentItem> {
    const space = resolveSpaceId(spaceId);
    if (isMockMode()) {
      const found = mockDocuments.find((d) => d.id === documentId);
      if (found) {
        found.status = 'pending';
        return found;
      }
      return mockDocuments[0];
    }
    return apiClient<DocumentItem>(
      `/spaces/${space}/documents/${documentId}/reprocess`,
      {
        method: 'POST',
      },
    );
  },

  async retryFailedDocumentsStream(
    spaceId: string,
    handlers: DocumentRetryHandlers,
    signal?: AbortSignal,
  ): Promise<void> {
    if (isMockMode()) {
      const failed = mockDocuments.filter(
        (d) => (d.space_id === spaceId || !spaceId) && d.status === 'failed',
      );
      handlers.onBegin?.({ total: failed.length });
      if (failed.length === 0) {
        handlers.onDone?.({ total: 0, retried: 0 });
        return;
      }
      let retried = 0;
      for (let i = 0; i < failed.length; i++) {
        const doc = failed[i];
        await new Promise((r) => setTimeout(r, 400));
        doc.status = 'ready';
        retried++;
        handlers.onProgress?.({
          document_id: doc.id,
          stage: 'retry',
          done: i + 1,
          total: failed.length,
          percent: Math.round(((i + 1) / failed.length) * 100),
        });
      }
      handlers.onDone?.({ total: failed.length, retried });
      return;
    }

    await fetchSSE({
      url: `/spaces/${spaceId}/documents/retry-failed`,
      method: 'POST',
      signal,
      handlers: {
        begin: (d) => handlers.onBegin?.(d as DocumentRetryBeginEvent),
        progress: (d) => handlers.onProgress?.(d as DocumentRetryProgressEvent),
        error: (d) => handlers.onError?.(d as DocumentRetryErrorEvent),
        done: (d) => handlers.onDone?.(d as DocumentRetryDoneEvent),
      },
    });
  },
};
