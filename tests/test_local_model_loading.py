"""本地模型离线优先加载，以及 jieba 词典的日志与预热。

全部用假的模型类与假的 hub 缓存查询，不加载任何真实权重、不联网。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import huggingface_hub
import jieba
import pytest
from structlog.testing import capture_logs

from agentmem.providers.adapters import local_embedding, local_rerank
from agentmem.store import fts
from agentmem.types import ProviderConfig


class _RecordingModel:
    """记录构造参数的假模型；``fail_offline`` 模拟缓存残缺时离线加载失败。"""

    calls: ClassVar[list[dict[str, Any]]] = []
    fail_offline: ClassVar[Exception | None] = None

    def __init__(self, model_name: str, **kwargs: Any) -> None:
        type(self).calls.append({"model": model_name, **kwargs})
        failure = type(self).fail_offline
        if kwargs.get("local_files_only") and failure is not None:
            raise failure

    def get_sentence_embedding_dimension(self) -> int:
        return 4


@pytest.fixture
def recording_model() -> type[_RecordingModel]:
    _RecordingModel.calls = []
    _RecordingModel.fail_offline = None
    return _RecordingModel


def _fake_cache(monkeypatch: pytest.MonkeyPatch, cached: set[str]) -> None:
    """让 hub 缓存查询只认 ``cached`` 里的仓库。"""

    def fake_lookup(repo_id: str, filename: str, **kwargs: Any) -> str | None:
        return f"/fake/{repo_id}/{filename}" if repo_id in cached else None

    monkeypatch.setattr(huggingface_hub, "try_to_load_from_cache", fake_lookup)


def test_cached_repo_is_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_cache(monkeypatch, {"BAAI/bge-m3"})
    assert local_embedding.is_cached_locally("BAAI/bge-m3") is True
    assert local_embedding.is_cached_locally("BAAI/bge-reranker-v2-m3") is False


def test_cache_marked_missing_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """hub 记下「这个文件不存在」时返回的是哨兵对象而不是路径，不能当成已缓存。"""
    monkeypatch.setattr(huggingface_hub, "try_to_load_from_cache", lambda *a, **k: object())
    assert local_embedding.is_cached_locally("BAAI/bge-m3") is False


def test_local_directory_counts_as_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_cache(monkeypatch, set())
    assert local_embedding.is_cached_locally(str(tmp_path)) is True


def test_lookup_error_falls_back_to_online(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: Any, **kwargs: Any) -> None:
        raise ValueError("不是合法的仓库 id")

    monkeypatch.setattr(huggingface_hub, "try_to_load_from_cache", broken)
    assert local_embedding.is_cached_locally("not a repo") is False


def test_cached_model_loads_offline(
    monkeypatch: pytest.MonkeyPatch, recording_model: type[_RecordingModel]
) -> None:
    _fake_cache(monkeypatch, {"BAAI/bge-m3"})
    with capture_logs() as logs:
        local_embedding.load_offline_first(recording_model, "BAAI/bge-m3", device="cpu")
    assert recording_model.calls == [
        {"model": "BAAI/bge-m3", "device": "cpu", "local_files_only": True}
    ]
    assert not [log for log in logs if log["event"] == "local_model_downloading"]


def test_missing_model_downloads_with_log(
    monkeypatch: pytest.MonkeyPatch, recording_model: type[_RecordingModel]
) -> None:
    _fake_cache(monkeypatch, set())
    with capture_logs() as logs:
        local_embedding.load_offline_first(recording_model, "BAAI/bge-m3", device="cpu")
    assert recording_model.calls == [{"model": "BAAI/bge-m3", "device": "cpu"}]
    assert [log["model"] for log in logs if log["event"] == "local_model_downloading"] == [
        "BAAI/bge-m3"
    ]


def test_incomplete_cache_retries_online(
    monkeypatch: pytest.MonkeyPatch, recording_model: type[_RecordingModel]
) -> None:
    """下载到一半被打断：config.json 在、权重不在，离线加载报 OSError，退回联网补全。"""
    _fake_cache(monkeypatch, {"BAAI/bge-m3"})
    recording_model.fail_offline = huggingface_hub.errors.LocalEntryNotFoundError("权重不在缓存里")
    with capture_logs() as logs:
        local_embedding.load_offline_first(recording_model, "BAAI/bge-m3")
    assert [call.get("local_files_only") for call in recording_model.calls] == [True, None]
    events = [log["event"] for log in logs]
    assert "local_model_cache_incomplete" in events
    assert "local_model_downloading" in events


def test_non_cache_error_is_not_masked_by_network(
    monkeypatch: pytest.MonkeyPatch, recording_model: type[_RecordingModel]
) -> None:
    """和缓存无关的错误（例如设备不支持某精度）原样抛出，不去联网重试。"""
    _fake_cache(monkeypatch, {"BAAI/bge-m3"})
    recording_model.fail_offline = TypeError("不支持的精度")
    with pytest.raises(TypeError):
        local_embedding.load_offline_first(recording_model, "BAAI/bge-m3")
    assert len(recording_model.calls) == 1


async def test_embedding_provider_loads_offline(
    monkeypatch: pytest.MonkeyPatch, recording_model: type[_RecordingModel]
) -> None:
    _fake_cache(monkeypatch, {"BAAI/bge-m3"})
    monkeypatch.setattr(local_embedding, "_AVAILABLE", True)
    monkeypatch.setattr(local_embedding, "SentenceTransformer", recording_model)
    monkeypatch.setattr(local_embedding, "_MODEL_CACHE", {})
    provider = local_embedding.LocalEmbeddingProvider(
        ProviderConfig(
            id="local-emb",
            kind="embedding",
            adapter="sentence_transformers",
            model="BAAI/bge-m3",
            device="cpu",
        )
    )
    await provider._get_model()
    assert recording_model.calls == [
        {"model": "BAAI/bge-m3", "device": "cpu", "local_files_only": True}
    ]


async def test_rerank_provider_loads_offline(
    monkeypatch: pytest.MonkeyPatch, recording_model: type[_RecordingModel]
) -> None:
    _fake_cache(monkeypatch, {"BAAI/bge-reranker-v2-m3"})
    monkeypatch.setattr(local_rerank, "_AVAILABLE", True)
    monkeypatch.setattr(local_rerank, "CrossEncoder", recording_model)
    monkeypatch.setattr(local_rerank, "_MODEL_CACHE", {})
    monkeypatch.setattr(local_rerank, "pick_dtype", lambda device: None)
    provider = local_rerank.LocalRerankProvider(
        ProviderConfig(
            id="local-rerank",
            kind="rerank",
            adapter="sentence_transformers_ce",
            model="BAAI/bge-reranker-v2-m3",
            device="cpu",
        )
    )
    await provider._get_model()
    assert recording_model.calls == [
        {"model": "BAAI/bge-reranker-v2-m3", "device": "cpu", "local_files_only": True}
    ]


def test_rerank_dtype_fallback_stays_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """半精度加载失败退回全精度时，第二次加载也不联网。"""
    calls: list[dict[str, Any]] = []

    class HalfPrecisionBroken:
        def __init__(self, model_name: str, **kwargs: Any) -> None:
            calls.append(kwargs)
            if "model_kwargs" in kwargs:
                raise RuntimeError("设备不支持半精度")

    _fake_cache(monkeypatch, {"BAAI/bge-reranker-v2-m3"})
    monkeypatch.setattr(local_rerank, "CrossEncoder", HalfPrecisionBroken)
    monkeypatch.setattr(local_rerank, "pick_dtype", lambda device: "float16")
    provider = local_rerank.LocalRerankProvider(
        ProviderConfig(
            id="local-rerank",
            kind="rerank",
            adapter="sentence_transformers_ce",
            model="BAAI/bge-reranker-v2-m3",
        )
    )
    provider._load("mps")
    assert [call.get("local_files_only") for call in calls] == [True, True]
    assert "model_kwargs" not in calls[-1]


def test_jieba_logs_only_through_app_logging() -> None:
    """jieba 自带的 stderr handler 已摘掉：否则每行日志都打两遍，看着像初始化了两次。"""
    assert jieba.log_console not in jieba.default_logger.handlers


def test_fts_warm_up_initializes_jieba(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[None] = []
    monkeypatch.setattr(jieba, "initialize", lambda: calls.append(None))
    elapsed = fts.warm_up()
    assert calls == [None]
    assert elapsed >= 0
