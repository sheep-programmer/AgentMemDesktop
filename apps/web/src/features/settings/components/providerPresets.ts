import type { ProviderKind, ProviderAdapter } from '@/lib/api/types.temp';

export interface AdapterOption {
  value: ProviderAdapter;
  label: string;
  isLocal?: boolean;
}

export const ADAPTERS_BY_KIND: Record<ProviderKind, AdapterOption[]> = {
  llm: [
    { value: 'openai_compatible', label: 'OpenAI 兼容协议 (OpenAI / DeepSeek / 智谱)' },
    { value: 'anthropic', label: 'Anthropic 原生协议 (Claude)' },
    { value: 'ollama_native', label: 'Ollama 本地原生协议' },
  ],
  embedding: [
    { value: 'sentence_transformers', label: 'Sentence Transformers (本地加载)', isLocal: true },
    { value: 'openai_compatible', label: 'OpenAI 兼容向量 API (如 text-embedding-3)' },
    { value: 'ollama_native', label: 'Ollama 本地向量 (如 nomic-embed-text)' },
  ],
  rerank: [
    { value: 'sentence_transformers_ce', label: 'Sentence Transformers Cross-Encoder (本地)', isLocal: true },
    { value: 'cohere_rerank', label: 'Cohere 重排 API' },
    { value: 'openai_compatible', label: 'OpenAI 兼容重排' },
  ],
};

export interface QuickPreset {
  label: string;
  baseUrl: string;
  model: string;
  kind?: ProviderKind;
}

export const QUICK_PRESETS: QuickPreset[] = [
  { label: 'DeepSeek', baseUrl: 'https://api.deepseek.com/v1', model: 'deepseek-chat', kind: 'llm' },
  { label: 'OpenAI', baseUrl: 'https://api.openai.com/v1', model: 'gpt-4o', kind: 'llm' },
  { label: '智谱 GLM', baseUrl: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-4-flash', kind: 'llm' },
  { label: '通义千问', baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen-plus', kind: 'llm' },
  { label: 'LM Studio (本地)', baseUrl: 'http://localhost:1234/v1', model: 'local-model', kind: 'llm' },
  { label: 'Ollama (兼容)', baseUrl: 'http://localhost:11434/v1', model: 'qwen2.5:latest', kind: 'llm' },
];

export const DEVICE_OPTIONS = [
  { value: 'auto', label: 'Auto (系统自动优选)' },
  { value: 'mps', label: 'Apple Silicon (MPS 加速)' },
  { value: 'cuda', label: 'NVIDIA GPU (CUDA)' },
  { value: 'cpu', label: 'CPU (通用运算)' },
];

export const COMMON_DIMENSIONS = [1536, 1024, 768, 512, 384];

export function isLocalAdapter(adapter: string): boolean {
  return adapter === 'sentence_transformers' || adapter === 'sentence_transformers_ce';
}

export function getDefaultAdapter(kind: ProviderKind): ProviderAdapter {
  switch (kind) {
    case 'embedding':
      return 'sentence_transformers';
    case 'rerank':
      return 'sentence_transformers_ce';
    case 'llm':
    default:
      return 'openai_compatible';
  }
}
