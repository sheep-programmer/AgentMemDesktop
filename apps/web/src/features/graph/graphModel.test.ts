import { describe, expect, it } from 'vitest';
import type { KnowledgeMap } from '@/lib/api/services/knowledgeMap';
import {
  bornBy, buildGraph, buildSteps, collideForce, cursorAtStep, isSolid,
  stepIndexAt, topicCountAt, topicMasteryAt,
} from './graphModel';

function fixture(): KnowledgeMap {
  return {
    space: { id: 's', name: '空间', domain: '领域' },
    nodes: [
      { id: 'domain:s', kind: 'domain', label: '领域', weight: 1 },
      { id: 'topic:0', kind: 'topic', label: '主题', weight: 1, mastery: 0.8 },
      { id: 'card:a', kind: 'card', label: '知识 A', weight: 0.9, mastery: 0.9, created_at: 100 },
      { id: 'card:b', kind: 'card', label: '知识 B', weight: 0.6, mastery: 0.6, created_at: 101 },
      { id: 'doc:a', kind: 'document', label: '资料', weight: 0.5, created_at: 10 },
    ],
    edges: [
      { src: 'domain:s', dst: 'topic:0', kind: 'topic' },
      { src: 'topic:0', dst: 'card:a', kind: 'covers' },
      { src: 'topic:0', dst: 'card:b', kind: 'covers' },
    ],
    topics: [{ topic: '主题', importance: 'core', subtopics: ['子主题'], covered: true, card_count: 6, mastery: 0.8 }],
    milestones: [{ at: 1000, kind: 'evolution', label: '进化完成' }],
    stats: { documents: 1, cards: 6, insights_active: 0, insights_candidate: 0, topics_total: 1, topics_covered: 1, overall: 0.8 },
    truncated: true,
  };
}

describe('成长时间轴', () => {
  it('保留集中导入和跨越很久的进化，按事件而非时间间隔分配节拍', () => {
    const steps = buildSteps(fixture());
    expect(steps).toEqual([10, 100, 101, 1000]);
    expect(stepIndexAt(steps, 9)).toBe(0);
    expect(stepIndexAt(steps, 100)).toBe(2);
    expect(stepIndexAt(steps, 500)).toBe(3);
    expect(stepIndexAt(steps, null)).toBe(4);
    expect(steps.map((_, index) => stepIndexAt(steps, cursorAtStep(steps, index)))).toEqual([0, 1, 2, 3]);
    expect(cursorAtStep(steps, 4)).toBeNull();
    expect(cursorAtStep([], 0)).toBeNull();
  });

  it('起点仅保留主题骨架与待补节点，卡片积累后逐渐增加掌握度', () => {
    const graph = buildGraph(fixture());
    expect(bornBy(graph.byId.get('topic:0')!, 9)).toBe(true);
    expect(bornBy(graph.byId.get('ghost:topic:0:0')!, 9)).toBe(true);
    expect(bornBy(graph.byId.get('card:a')!, 9)).toBe(false);
    expect(topicCountAt(graph, 'topic:0', 9)).toBe(0);
    expect(topicMasteryAt(graph, 'topic:0', 9)).toBe(0);
    expect(topicCountAt(graph, 'topic:0', 100)).toBe(1);
    expect(topicMasteryAt(graph, 'topic:0', 100)).toBeCloseTo(0.3);
    expect(topicMasteryAt(graph, 'topic:0', 101)).toBeCloseTo(0.5);
  });

  it('现在的掌握度计入画布上限外的卡片', () => {
    const graph = buildGraph(fixture());
    expect(topicCountAt(graph, 'topic:0', null)).toBe(6);
    expect(topicMasteryAt(graph, 'topic:0', null)).toBe(0.8);
  });
});

describe('知识更新与布局', () => {
  it('刷新保留已有位置和待补槽位位置，新知识保留真实归属', () => {
    const before = buildGraph(fixture());
    Object.assign(before.byId.get('topic:0')!, { x: 245, y: 132, vx: 4, vy: -8 });
    Object.assign(before.byId.get('ghost:topic:0:2')!, { x: 300, y: 160 });
    const data = fixture();
    data.nodes!.push({ id: 'card:c', kind: 'card', label: '知识 C', weight: 1, mastery: 1, created_at: 1100 });
    data.edges!.push({ src: 'topic:0', dst: 'card:c', kind: 'covers' });
    const after = buildGraph(data, before.byId);
    expect(after.byId.get('topic:0')).toMatchObject({ x: 245, y: 132, vx: 0, vy: 0 });
    expect(after.byId.get('ghost:topic:0:2')).toMatchObject({ x: 300, y: 160 });
    expect(after.cardTopic.get('card:c')).toBe('topic:0');
    expect(after.neighbors.get('topic:0')?.has('card:c')).toBe(true);
  });

  it('把完全重合的节点推开，同时保持领域锚点固定', () => {
    const graph = buildGraph(fixture());
    const domain = graph.byId.get('domain:s')!;
    const card = graph.byId.get('card:a')!;
    Object.assign(card, { x: 0, y: 0 });
    const collide = collideForce(() => 10);
    collide.initialize([domain, card]);
    collide(1);
    expect(domain.vx ?? 0).toBe(0);
    expect(domain.vy ?? 0).toBe(0);
    expect(Math.hypot(card.vx ?? 0, card.vy ?? 0)).toBeGreaterThan(0);
  });

  it('不会把大量低置信度知识标为扎实', () => {
    expect(isSolid(15, 0.35)).toBe(false);
    expect(isSolid(2, 0.95)).toBe(false);
    expect(isSolid(3, 0.85)).toBe(true);
  });
});
