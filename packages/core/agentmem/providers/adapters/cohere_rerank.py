"""Cohere 规范的云端 Rerank 适配器（兼容 SiliconFlow / Voyage）。"""

from __future__ import annotations

import time
from typing import Any

import httpx
import structlog

from agentmem.errors import ProviderTimeoutError, ProviderUnavailableError
from agentmem.providers.base import RankedDoc
from agentmem.types import ProviderConfig, ProviderHealth

logger = structlog.get_logger(__name__)

DEFAULT_BASE_URL = "https://api.cohere.com/v2"
DEFAULT_TIMEOUT = 60.0


class CohereRerankProvider:
    """调用 ``POST {base_url}/rerank``。"""

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        self.name = config.id
        self.model = config.model or "rerank-v3.5"
        self.base_url = (config.base_url or DEFAULT_BASE_URL).rstrip("/")
        self._timeout = float(config.extra.get("timeout", DEFAULT_TIMEOUT))

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    async def rerank(self, query: str, docs: list[str], *, top_n: int) -> list[RankedDoc]:
        """调用云端重排接口。"""
        if not docs:
            return []
        payload: dict[str, Any] = {
            "model": self.model,
            "query": query,
            "documents": docs,
            "top_n": min(top_n, len(docs)),
        }
        url = f"{self.base_url}/rerank"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(url, json=payload, headers=self._headers())
                response.raise_for_status()
                data = response.json()
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                f"{self.name} 响应超时", detail={"provider_id": self.name, "url": url}
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise ProviderUnavailableError(
                f"{self.name} 返回 HTTP {exc.response.status_code}",
                detail={
                    "provider_id": self.name,
                    "url": url,
                    "body": exc.response.text[:500],
                },
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                f"{self.name} 无法连接：{exc}", detail={"provider_id": self.name, "url": url}
            ) from exc
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            return []
        ranked: list[RankedDoc] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            index = item.get("index")
            score = item.get("relevance_score")
            if not isinstance(index, int):
                continue
            ranked.append(
                RankedDoc(
                    index=index,
                    score=float(score) if isinstance(score, (int, float)) else 0.0,
                    text=docs[index] if 0 <= index < len(docs) else None,
                )
            )
        ranked.sort(key=lambda doc: doc.score, reverse=True)
        return ranked

    async def health(self) -> ProviderHealth:
        """最小重排请求实测连通性。"""
        started = time.perf_counter()
        try:
            ranked = await self.rerank("ping", ["ping"], top_n=1)
            latency = int((time.perf_counter() - started) * 1000)
            if not ranked:
                return ProviderHealth(ok=False, latency_ms=latency, error="未返回打分")
            return ProviderHealth(ok=True, latency_ms=latency, resolved_model=self.model)
        except Exception as exc:
            latency = int((time.perf_counter() - started) * 1000)
            logger.warning("provider_health_failed", provider=self.name, error=str(exc))
            return ProviderHealth(ok=False, latency_ms=latency, error=str(exc))
