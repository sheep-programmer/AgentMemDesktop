"""本地 Embedding 适配器（sentence-transformers）。

``sentence-transformers`` 依赖 PyTorch，体积大，因此采用**可选导入**：未安装时本模块
仍可正常 import，但 :func:`is_available` 返回 ``False``，实例化并调用会抛
``PROVIDER_UNAVAILABLE``。
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import structlog

from agentmem.errors import ProviderUnavailableError
from agentmem.providers.base import EmbedKind
from agentmem.types import ProviderConfig, ProviderHealth

logger = structlog.get_logger(__name__)

_IMPORT_ERROR: str | None = None
try:  # pragma: no cover - 取决于是否安装可选依赖
    from sentence_transformers import SentenceTransformer

    _AVAILABLE = True
except Exception as exc:
    SentenceTransformer = None
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
    _AVAILABLE = False


def is_available() -> bool:
    """本地 embedding 依赖是否可用。"""
    return _AVAILABLE


def unavailable_reason() -> str | None:
    """依赖不可用时的原因。"""
    return _IMPORT_ERROR


#: 英文 BGE 系列查询侧需要加指令前缀；bge-m3 不需要
_QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "


def default_prefixes(model_name: str) -> tuple[str, str]:
    """按模型族返回 (query_prefix, doc_prefix)。"""
    name = model_name.lower()
    if name.startswith("intfloat/e5") or "/e5-" in name or name.startswith("e5-"):
        return "query: ", "passage: "
    if name.startswith("bge-") and "m3" not in name:
        return _QUERY_INSTRUCTION, ""
    return "", ""


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


def pick_dtype(device: str) -> Any:
    """按设备选择推理精度；不支持半精度的设备返回 ``None``（即全精度）。"""
    try:  # pragma: no cover - 取决于本机环境
        import torch

        if device.startswith(("cuda", "mps")):
            return torch.float16
    except Exception:
        return None
    return None


def pick_embedding_dtype(device: str) -> Any:
    """向量模型的精度。

    刻意不跟随 :func:`pick_dtype`：向量会落进索引，换精度等于换一套坐标，旧向量与新
    向量就没法比了。要改得整库重算。
    """
    try:  # pragma: no cover - 取决于本机环境
        import torch

        if device.startswith("cuda"):
            return torch.float16
    except Exception:
        return None
    return None


def is_cached_locally(model_name: str) -> bool:
    """模型是否已在本地：本地目录，或 Hugging Face 缓存里已有这个仓库的 ``config.json``。

    缓存位置与 sentence-transformers 自己的取法一致：``SENTENCE_TRANSFORMERS_HOME``
    优先，否则是 huggingface_hub 的默认缓存（``HF_HUB_CACHE``）。只看 ``config.json``
    是有意的粗判——缓存残缺时离线加载会报错，由 :func:`load_offline_first` 退回联网。
    """
    if Path(model_name).expanduser().is_dir():
        return True
    try:
        from huggingface_hub import try_to_load_from_cache

        found = try_to_load_from_cache(
            model_name, "config.json", cache_dir=os.getenv("SENTENCE_TRANSFORMERS_HOME")
        )
    except Exception:
        # 模型名不是合法的仓库 id 之类：判不了就当没缓存，照常走联网加载
        return False
    return isinstance(found, str)


def load_offline_first(factory: Callable[..., Any], model_name: str, **kwargs: Any) -> Any:
    """本地有缓存就离线加载，没有才联网下载。

    不加限制时 sentence-transformers 每次加载都会先向 huggingface.co 逐个文件发 HEAD
    校验、再查一遍仓库信息（实测一次启动两个模型共 600 多个请求）：网络慢时拖慢加载，
    断网时还会卡在重试上——对「本地优先」的应用说不过去。

    离线用的是构造参数 ``local_files_only=True``（sentence-transformers 6.x 的
    ``SentenceTransformer`` / ``CrossEncoder`` 都支持，并一路传给 hub 下载与
    transformers 的模型、分词器加载），而不是改 ``HF_HUB_OFFLINE``：那是进程级环境变量，
    huggingface_hub 在导入时就读进常量，临时改既不一定生效，又会波及同进程的其他下载。

    缓存残缺（例如下载到一半被打断）时离线加载抛 ``OSError``（``LocalEntryNotFoundError``
    也是它的子类），这时退回联网补全；其他异常原样抛出，不拿网络去掩盖真正的问题。
    """
    if is_cached_locally(model_name):
        try:
            return factory(model_name, local_files_only=True, **kwargs)
        except OSError as exc:
            logger.warning("local_model_cache_incomplete", model=model_name, error=str(exc))
    logger.info(
        "local_model_downloading",
        model=model_name,
        hint="本地缓存里没有，正在从 Hugging Face 下载；首次下载约 2GB，可能要几分钟",
    )
    return factory(model_name, **kwargs)


#: 进程级的模型缓存：(模型名, 设备) → 已加载的模型。
#: 每个 Space 有各自的 ProviderRegistry，实例缓存也是各自的；不共享的话有几个 Space
#: 就会把 bge-m3 加载几份（实测日志里 `local_embedding_loaded` 出现两次，每份约 2GB），
#: 预热也因此失效——预热的是全局 registry，请求用的是 Space 自己的那个。
_MODEL_CACHE: dict[tuple[str, str], Any] = {}

#: 本地推理的设备互斥。同一块设备上的并发推理会互相争用（MPS 上实测段错误），
#: 而同一个进程里可能有多个 Space 各自的 provider 实例，所以锁放在模块级；
#: 向量模型与重排模型共用这一把。
DEVICE_LOCK = asyncio.Lock()

#: 本地模型的加载互斥，向量与重排共用。
#:
#: 每个 provider 实例原本各有一把加载锁，而启动预热用的是全局 registry 的实例、
#: 提问用的是 Space 自己的实例——预热还没加载完（约 30 秒）时进来的第一个问题，
#: 查缓存没命中，就会自己再加载一份：多等半分钟、多占一份内存，两份权重还会同时往
#: Metal 上搬。放到模块级后，后来者等在锁上，拿到锁再查一次缓存，直接复用预热那份。
MODEL_LOAD_LOCK = asyncio.Lock()


class LocalEmbeddingProvider:
    """本地 sentence-transformers 向量化。

    并发调用不安全：多批同时推理会争用同一块设备（MPS 上实测段错误）。摄取侧只在
    ``concurrent_safe`` 为真时才并发取向量，这里再上一道设备锁——多篇文档同时摄取时，
    每个文档自己的批是串行的，但文档之间仍会撞上同一个模型。
    """

    concurrent_safe = False

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        self.name = config.id
        self.model_name = config.model or "BAAI/bge-m3"
        self.dimension = config.dimension or 0
        self._model: Any | None = None
        self._load_lock = asyncio.Lock()
        configured_query = config.extra.get("query_prefix")
        configured_doc = config.extra.get("doc_prefix")
        default_query, default_doc = default_prefixes(self.model_name)
        self.query_prefix = str(configured_query) if configured_query is not None else default_query
        self.doc_prefix = str(configured_doc) if configured_doc is not None else default_doc

    @property
    def device(self) -> str:
        """实际使用的设备。"""
        return resolve_device(str(self.config.device or "auto"))

    async def _get_model(self) -> Any:
        if not _AVAILABLE:
            raise ProviderUnavailableError(
                "未安装 sentence-transformers，本地 Embedding 不可用",
                detail={"provider_id": self.name, "reason": _IMPORT_ERROR},
            )
        # 已加载就直接用：共用的加载锁只该挡住「正在加载」，不能让另一个模型的
        # 半分钟加载把已经就绪的这个也卡住
        if self._model is not None:
            return self._model
        async with self._load_lock, MODEL_LOAD_LOCK:
            if self._model is None:
                device = self.device
                cache_key = (self.model_name, device)
                cached = _MODEL_CACHE.get(cache_key)
                if cached is not None:
                    self._model = cached
                else:
                    kwargs: dict[str, Any] = {"device": device}
                    dtype = pick_embedding_dtype(device)
                    if dtype is not None:
                        kwargs["model_kwargs"] = {"dtype": dtype}
                    model = await asyncio.to_thread(
                        load_offline_first, SentenceTransformer, self.model_name, **kwargs
                    )
                    _MODEL_CACHE[cache_key] = model
                    self._model = model
                    logger.info(
                        "local_embedding_loaded",
                        provider=self.name,
                        model=self.model_name,
                        device=device,
                        dimension=int(model.get_sentence_embedding_dimension()),
                    )
                if not self.dimension:
                    self.dimension = int(self._model.get_sentence_embedding_dimension())
        return self._model

    async def embed(self, texts: list[str], *, kind: EmbedKind = "doc") -> list[list[float]]:
        """向量化；按 doc / query 追加各自前缀。"""
        if not texts:
            return []
        model = await self._get_model()
        prefix = self.query_prefix if kind == "query" else self.doc_prefix
        payload = [prefix + text for text in texts]
        batch_size = int(self.config.extra.get("batch_size", 32))
        normalize = bool(self.config.extra.get("normalize", True))
        async with DEVICE_LOCK:
            vectors = await self._encode(model, payload, batch_size, normalize)
        return vectors

    async def _encode(
        self,
        model: Any,
        payload: list[str],
        batch_size: int,
        normalize: bool,
    ) -> list[list[float]]:
        """真正跑推理；调用方负责持有设备锁。"""
        vectors = await asyncio.to_thread(
            model.encode,
            payload,
            batch_size=batch_size,
            normalize_embeddings=normalize,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        result = [[float(value) for value in row] for row in vectors]
        if result and not self.dimension:
            self.dimension = len(result[0])
        return result

    async def health(self) -> ProviderHealth:
        """加载模型并做一次向量化。"""
        started = time.perf_counter()
        try:
            vectors = await self.embed(["ping"])
            latency = int((time.perf_counter() - started) * 1000)
            if not vectors:
                return ProviderHealth(ok=False, latency_ms=latency, error="未返回向量")
            return ProviderHealth(
                ok=True,
                latency_ms=latency,
                resolved_model=f"{self.model_name} ({self.device})",
            )
        except Exception as exc:
            latency = int((time.perf_counter() - started) * 1000)
            logger.warning("provider_health_failed", provider=self.name, error=str(exc))
            return ProviderHealth(ok=False, latency_ms=latency, error=str(exc))
