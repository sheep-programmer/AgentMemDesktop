import { apiClient, isMockMode } from '../client';
import type { components } from '../types.gen';
import {
  mockDocuments,
  mockEvolveHistory,
  mockInsights,
  mockKnowledgeCards,
  mockKnowledgeGapsTree,
  mockSpaces,
} from '../mock/data';

export type KnowledgeMap = components['schemas']['KnowledgeMapResponse'];
export type KnowledgeMapNode = components['schemas']['KnowledgeMapNode'];
export type KnowledgeMapEdge = components['schemas']['KnowledgeMapEdge'];
export type KnowledgeMapTopic = components['schemas']['KnowledgeMapTopic'];
export type KnowledgeMapMilestone = components['schemas']['KnowledgeMapMilestone'];

/**
 * 演示模式下的知识网络：用 mock 里已有的资料、卡片、经验与大纲拼出来，
 * 口径与后端一致（主题按关键词命中卡片标题 / 别名算覆盖）。
 */
function mockKnowledgeMap(spaceId: string): KnowledgeMap {
  const space = mockSpaces.find((item) => item.id === spaceId) ?? mockSpaces[0];
  const outline = mockKnowledgeGapsTree[0]?.children ?? [];
  const nodes: KnowledgeMapNode[] = [];
  const edges: KnowledgeMapEdge[] = [];
  const domainId = `domain:${space.id}`;
  nodes.push({
    id: domainId,
    kind: 'domain',
    label: space.name,
    weight: 1,
    mastery: null,
    ref_id: space.id,
  });

  const cards = mockKnowledgeCards.filter((card) => card.space_id === space.id);
  const topics = outline.flatMap((group) => group.children ?? [group]);
  const topicRows = topics.map((topic, index) => {
    const title = topic.title ?? '未命名主题';
    const childTitles = (topic.children ?? []).map((child) => child.title ?? '').filter(Boolean);
    const words = title.split(/[\s/、与和（）()]+/).filter((word) => word.length >= 2);
    const matched = cards.filter((card) =>
      words.some((word) => `${card.title} ${(card.aliases ?? []).join(' ')}`.includes(word)),
    );
    const avg = matched.length
      ? matched.reduce((sum, card) => sum + card.confidence, 0) / matched.length
      : 0;
    const mastery = Math.min(1, matched.length / 3) * avg;
    const id = `topic:${index}`;
    nodes.push({
      id,
      kind: 'topic',
      label: title,
      weight: index < 3 ? 1 : 0.7,
      mastery,
      status: matched.length ? 'covered' : 'uncovered',
      detail: childTitles.join('、') || null,
    });
    edges.push({ src: domainId, dst: id, kind: 'topic' });
    matched.forEach((card) => edges.push({ src: id, dst: `card:${card.id}`, kind: 'covers' }));
    return {
      topic: title,
      importance: index < 3 ? 'core' : 'common',
      subtopics: childTitles,
      covered: matched.length > 0,
      card_count: matched.length,
      mastery,
    };
  });

  const docs = mockDocuments.filter((doc) => doc.space_id === space.id);
  docs.forEach((doc) => {
    nodes.push({
      id: `doc:${doc.id}`,
      kind: 'document',
      label: doc.title,
      created_at: doc.created_at,
      weight: 0.6,
      status: doc.status,
      ref_id: doc.id,
    });
    edges.push({ src: domainId, dst: `doc:${doc.id}`, kind: 'source' });
  });
  cards.forEach((card, index) => {
    nodes.push({
      id: `card:${card.id}`,
      kind: 'card',
      label: card.title,
      created_at: card.created_at,
      mastery: card.confidence,
      weight: card.confidence,
      status: card.kind,
      ref_id: card.id,
    });
    const doc = docs[index % Math.max(1, docs.length)];
    if (doc) edges.push({ src: `doc:${doc.id}`, dst: `card:${card.id}`, kind: 'source' });
  });
  const insights = mockInsights.filter(
    (insight) => insight.space_id === space.id && insight.status !== 'archived',
  );
  insights.forEach((insight, index) => {
    nodes.push({
      id: `insight:${insight.id}`,
      kind: 'insight',
      label: insight.trigger,
      created_at: insight.created_at,
      mastery: insight.confidence,
      weight: insight.confidence,
      status: insight.status,
      detail: insight.guidance.slice(0, 120),
      ref_id: insight.id,
    });
    const card = cards[index % Math.max(1, cards.length)];
    edges.push({
      src: card ? `card:${card.id}` : domainId,
      dst: `insight:${insight.id}`,
      kind: 'insight',
    });
  });

  const milestones = [
    ...docs.map((doc) => ({
      at: doc.created_at,
      kind: 'document' as const,
      label: `导入资料：${doc.title}`,
      value: null,
    })),
    ...mockEvolveHistory.map((run) => ({
      at: run.run_at ?? Date.now(),
      kind: 'evolution' as const,
      label: '完成一次进化',
      value: run.expertise_after ?? null,
    })),
    ...insights
      .filter((insight) => insight.status === 'active')
      .map((insight) => ({
        at: insight.updated_at,
        kind: 'insight' as const,
        label: `经验生效：${insight.trigger}`,
        value: insight.confidence,
      })),
  ].sort((a, b) => a.at - b.at);

  const covered = topicRows.filter((row) => row.covered).length;
  return {
    space: { id: space.id, name: space.name, domain: space.domain },
    nodes,
    edges,
    topics: topicRows,
    milestones,
    stats: {
      documents: docs.length,
      cards: cards.length,
      insights_active: insights.filter((insight) => insight.status === 'active').length,
      insights_candidate: insights.filter((insight) => insight.status === 'candidate').length,
      topics_total: topicRows.length,
      topics_covered: covered,
      overall: topicRows.length
        ? topicRows.reduce((sum, row) => sum + row.mastery, 0) / topicRows.length
        : null,
    },
    truncated: false,
  };
}

export const knowledgeMapService = {
  async getKnowledgeMap(
    spaceId: string,
    options?: { maxCards?: number; signal?: AbortSignal },
  ): Promise<KnowledgeMap> {
    if (isMockMode()) return mockKnowledgeMap(spaceId);
    const params = new URLSearchParams();
    if (options?.maxCards) params.set('max_cards', String(options.maxCards));
    const qs = params.toString();
    return apiClient<KnowledgeMap>(
      `/spaces/${spaceId}/knowledge-map${qs ? `?${qs}` : ''}`,
      { signal: options?.signal },
    );
  },
};
