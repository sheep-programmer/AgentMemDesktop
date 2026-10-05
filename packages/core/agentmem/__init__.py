"""AgentMem core：本地优先的可进化知识库内核。

本包不依赖任何 Web 框架，可被 CLI、脚本、Jupyter 直接使用。
"""

from agentmem.config import Settings, get_settings
from agentmem.errors import AgentMemError
from agentmem.types import Space, SpaceCreate

__version__ = "0.1.0"

__all__ = ["AgentMemError", "Settings", "Space", "SpaceCreate", "__version__", "get_settings"]
