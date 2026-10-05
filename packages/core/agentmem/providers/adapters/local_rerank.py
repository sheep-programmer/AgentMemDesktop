"""本地 Rerank 适配器（sentence-transformers CrossEncoder）。

与本地 embedding 一样，``sentence-transformers`` 为可选依赖。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

from agentmem.errors import ProviderUnavailableError, ValidationError
from agentmem.providers.adapters.local_embedding import (
    DEVICE_LOCK,
    MODEL_LOAD_LOCK,
    load_offline_first,
    pick_dtype,
)
from agentmem.providers.base import RankedDoc
from agentmem.types import ProviderConfig, ProviderHealth

logger = structlog.get_logger(__name__)

_IMPORT_ERROR: str | None = None
try:  # pragma: no cover - 取决于是否安装可选依赖
    from sentence_transformers import CrossEncoder

    _AVAILABLE = True
except Exception as exc:
    CrossEncoder = None
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
    _AVAILABLE = False


def is_available() -> bool:
    """本地 rerank 依赖是否可用。"""
    return _AVAILABLE


def unavailable_reason() -> str | None:
    """依赖不可用时的原因。"""
    return _IMPORT_ERROR


def resolve_device(preference: str = "auto") -> str:
    """解析运行设备；``auto`` 依次尝试 mps → cuda → cpu。"""
    if preference and preference != "auto":
        return preference
    try:  # pragma: no cover - 取决于本机环境
        import torch

        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


#: 与向量模型同理：进程级共享，避免每个 Space 各加载一份。
_MODEL_CACHE: dict[tuple[str, str], Any] = {}


class LocalRerankProvider:
    """本地 CrossEncoder 重排。

    与本地向量模型同理：进程内推理不能并发，一次只能跑一个。
    """

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        self.name = config.id
        self.model_name = config.model or "BAAI/bge-reranker-v2-m3"
        self._model: Any | None = None
        self._load_lock = asyncio.Lock()

    @property
    def device(self) -> str:
        """实际使用的设备。"""
        return resolve_device(str(self.config.device or "auto"))

    async def _get_model(self) -> Any:
        if not _AVAILABLE:
            raise ProviderUnavailableError(
                "未安装 sentence-transformers，本地 Rerank 不可用",
                detail={"provider_id": self.name, "reason": _IMPORT_ERROR},
            )
        # 已加载就直接用：共用的加载锁只该挡住「正在加载」，不能让另一个模型的
        # 半分钟加载把已经就绪的这个也卡住
        if self._model is not None:
            return self._model
        # 加载锁与向量模型共用，理由见 ``local_embedding.MODEL_LOAD_LOCK``
        async with self._load_lock, MODEL_LOAD_LOCK:
            if self._model is None:
                device = self.device
                cache_key = (self.model_name, device)
                cached = _MODEL_CACHE.get(cache_key)
                if cached is not None:
                    self._model = cached
                else:
                    model = await asyncio.to_thread(self._load, device)
                    _MODEL_CACHE[cache_key] = model
                    self._model = model
                    logger.info(
                        "local_rerank_loaded",
                        provider=self.name,
                        model=self.model_name,
                        device=device,
                    )
        return self._model

    def _load(self, device: str) -> Any:
        """加载交叉编码器，按设备选推理精度。

        半精度在 Apple 芯片上实测快约两成，打分差异在万分之几量级。加载失败就退回
        全精度——设备对半精度的支持并不整齐，宁可慢也不要起不来。

        向量模型不走这条路：它的输出会落进索引，换精度等于换了一套坐标。

        两次尝试都走 :func:`load_offline_first`：本地有缓存就不联网。
        """
        dtype = pick_dtype(device)
        if dtype is None:
            return load_offline_first(CrossEncoder, self.model_name, device=device)
        try:
            return load_offline_first(
                CrossEncoder, self.model_name, device=device, model_kwargs={"dtype": dtype}
            )
        except Exception as exc:  # pragma: no cover - 取决于本机设备
            logger.info("local_rerank_dtype_fallback", provider=self.name, error=str(exc))
            return load_offline_first(CrossEncoder, self.model_name, device=device)

    async def rerank(self, query: str, docs: list[str], *, top_n: int) -> list[RankedDoc]:
        """按 (query, doc) 打分并返回前 ``top_n`` 条。"""
        if not docs:
            return []
        if top_n < 1:
            raise ValidationError("top_n 必须为正整数", detail={"top_n": top_n})
        model = await self._get_model()
        pairs = [(query, doc) for doc in docs]
        batch_size = int(self.config.extra.get("batch_size", 16))
        async with DEVICE_LOCK:
            scores = await asyncio.to_thread(
                model.predict, pairs, batch_size=batch_size, show_progress_bar=False
            )
        ranked = [
            RankedDoc(index=index, score=float(score), text=docs[index])
            for index, score in enumerate(scores)
        ]
        ranked.sort(key=lambda item: item.score, reverse=True)
        return ranked[:top_n]

    async def health(self) -> ProviderHealth:
        """加载模型并做一次打分。"""
        started = time.perf_counter()
        try:
            ranked = await self.rerank("ping", ["ping"], top_n=1)
            latency = int((time.perf_counter() - started) * 1000)
            if not ranked:
                return ProviderHealth(ok=False, latency_ms=latency, error="未返回打分")
            return ProviderHealth(
                ok=True,
                latency_ms=latency,
                resolved_model=f"{self.model_name} ({self.device})",
            )
        except Exception as exc:
            latency = int((time.perf_counter() - started) * 1000)
            logger.warning("provider_health_failed", provider=self.name, error=str(exc))
            return ProviderHealth(ok=False, latency_ms=latency, error=str(exc))
