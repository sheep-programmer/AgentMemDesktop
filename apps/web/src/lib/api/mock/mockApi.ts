/**
 * AgentMem · Mock API 处理器与流式发生器
 */

import {
  mockSpaces,
  mockDocuments,
  mockKnowledgeCards,
  mockGraphData,
  mockInsights,
  mockConflictGroups,
  mockConversations,
  mockMessages,
  mockTraceDetail,
  mockEvolvePending,
  mockEvolveHistory,
  mockCurrentExpertise,
  mockPreviousExpertise,
  mockKnowledgeGapsTree,
  mockEvalItems,
  mockEvalRuns,
  mockProviders,
  mockRoleBindings,
  mockCapabilities,
  mockSystemStats,
} from './data';
import type {
  Space,
  DocumentItem,
  KnowledgeCard,
  Insight,
  Conversation,
  Message,
} from '../types.temp';

const delay = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

export const mockApi = {
  // System
  async getCapabilities() {
    await delay(120);
    return mockCapabilities;
  },
  async getStats() {
    await delay(150);
    return mockSystemStats;
  },

  // Spaces
  async getSpaces(): Promise<Space[]> {
    await delay(150);
    return [...mockSpaces];
  },
  async getSpace(id: string): Promise<Space | undefined> {
    await delay(100);
    return mockSpaces.find((s) => s.id === id) || mockSpaces[0];
  },

  // Documents
  async getDocuments(_spaceId: string): Promise<DocumentItem[]> {
    await delay(200);
    return [...mockDocuments];
  },

  // Knowledge
  async getKnowledgeCards(_spaceId: string): Promise<KnowledgeCard[]> {
    await delay(200);
    return [...mockKnowledgeCards];
  },
  async getGraphData(_spaceId: string) {
    await delay(250);
    return mockGraphData;
  },

  // Insights
  async getInsights(_spaceId: string): Promise<Insight[]> {
    await delay(180);
    return [...mockInsights];
  },
  async getConflictGroups(_spaceId: string) {
    await delay(150);
    return [...mockConflictGroups];
  },

  // Conversations
  async getConversations(_spaceId: string): Promise<Conversation[]> {
    await delay(150);
    return [...mockConversations];
  },
  async getMessages(_conversationId: string): Promise<Message[]> {
    await delay(150);
    return [...mockMessages];
  },
  async getTrace(_traceId: string) {
    await delay(120);
    return mockTraceDetail;
  },

  // Evolve
  async getEvolvePending(_spaceId: string) {
    await delay(120);
    return mockEvolvePending;
  },
  async getEvolveHistory(_spaceId: string) {
    await delay(200);
    return [...mockEvolveHistory];
  },

  // Expertise
  async getExpertise(_spaceId: string) {
    await delay(180);
    return {
      current: mockCurrentExpertise,
      previous: mockPreviousExpertise,
    };
  },
  async getKnowledgeGaps(_spaceId: string) {
    await delay(200);
    return mockKnowledgeGapsTree;
  },
  async getEvalItems(_spaceId: string) {
    await delay(160);
    return [...mockEvalItems];
  },
  async getEvalRuns(_spaceId: string) {
    await delay(180);
    return [...mockEvalRuns];
  },

  // Providers & Roles
  async getProviders() {
    await delay(150);
    return [...mockProviders];
  },
  async getRoleBindings() {
    await delay(120);
    return { ...mockRoleBindings };
  },
};
