"""模型可插拔层：Protocol 定义、注册表、适配器、健康检查与模型发现。"""

from agentmem.providers.base import (
    ChatChunk,
    ChatResult,
    EmbeddingProvider,
    HealthCheckable,
    LLMProvider,
    Message,
    RankedDoc,
    RerankProvider,
    ToolCall,
    ToolSpec,
)
from agentmem.providers.discover import (
    discover_models,
    discover_ollama,
    discover_openai,
)
from agentmem.providers.health import check, check_config
from agentmem.providers.registry import ADAPTERS, ProviderRegistry
from agentmem.types import ProviderHealth

__all__ = [
    "ADAPTERS",
    "ChatChunk",
    "ChatResult",
    "EmbeddingProvider",
    "HealthCheckable",
    "LLMProvider",
    "Message",
    "ProviderHealth",
    "ProviderRegistry",
    "RankedDoc",
    "RerankProvider",
    "ToolCall",
    "ToolSpec",
    "check",
    "check_config",
    "discover_models",
    "discover_ollama",
    "discover_openai",
]
