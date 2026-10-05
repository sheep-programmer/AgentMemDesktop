"""本机 Agent 配置复用的测试。

这是**读取用户凭据文件**的模块，安全不变量比功能本身更重要：
明文密钥绝不能出现在返回结构里，也绝不能被写进日志。
每条测试都对应一条安全约束或一种真实配置形态。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentmem.providers import local_agents
from agentmem.providers.local_agents import resolve_api_key, scan_local_agents
from agentmem.types import LocalAgentCandidate

LITERAL_KEY = "sk-super-secret-value-1234"


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 HOME 指向临时目录，测试绝不碰真实用户配置。"""
    monkeypatch.setattr(local_agents, "_home", lambda: tmp_path)
    # 清掉可能影响判定的真实环境变量
    for name, *_ in local_agents.ENV_PROVIDERS:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.replace("_API_KEY", "_BASE_URL"), raising=False)
    return tmp_path


def _write_claude(home: Path, payload: dict[str, object]) -> Path:
    path = home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_codex(home: Path, content: str) -> Path:
    path = home / ".codex" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ------------------------------------------------------------------ 安全不变量


def test_literal_key_never_leaves_as_plaintext(fake_home: Path) -> None:
    """配置里是明文密钥时，返回结构里只能有掩码，绝不能有明文。"""
    _write_claude(fake_home, {"env": {"ANTHROPIC_API_KEY": LITERAL_KEY}})

    result = scan_local_agents()
    dumped = result.model_dump_json()

    assert LITERAL_KEY not in dumped, "明文密钥泄漏到了响应结构里"
    candidate = next(c for c in result.candidates if c.source == "claude_code")
    assert candidate.has_api_key is True
    assert candidate.api_key_hint is not None
    assert candidate.api_key_hint.endswith("1234")  # 只保留尾 4 位
    assert LITERAL_KEY not in (candidate.api_key_hint or "")


def test_candidate_model_has_no_plaintext_field() -> None:
    """结构上就不给明文密钥留位置——防止后续有人「顺手」加回来。"""
    fields = set(LocalAgentCandidate.model_fields)
    assert "api_key" not in fields
    assert fields >= {"api_key_env", "api_key_hint", "has_api_key"}


def test_resolve_api_key_returns_placeholder_not_plaintext(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """导入时写入的是 ``${VAR}`` 占位符，不是明文——明文永不落盘。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", LITERAL_KEY)
    result = scan_local_agents()
    candidate = next(c for c in result.candidates if c.api_key_env == "DEEPSEEK_API_KEY")

    assert resolve_api_key(candidate) == "${DEEPSEEK_API_KEY}"


def test_resolve_api_key_none_when_no_source(fake_home: Path) -> None:
    """没有可用密钥来源时返回 None，由用户手动补，而不是瞎猜。"""
    _write_claude(fake_home, {"env": {"ANTHROPIC_BASE_URL": "https://proxy.example.com"}})
    result = scan_local_agents()
    candidate = next(c for c in result.candidates if c.source == "claude_code")

    assert candidate.api_key_env is None
    assert resolve_api_key(candidate) is None
    assert candidate.note and "手动" in candidate.note


def test_scan_reports_paths_for_transparency(fake_home: Path) -> None:
    """必须告诉用户到底读了哪些文件。"""
    path = _write_claude(fake_home, {"env": {"ANTHROPIC_MODEL": "claude-sonnet-4-5"}})
    result = scan_local_agents()
    assert str(path) in result.scanned_paths


def test_scan_never_touches_files_outside_whitelist(fake_home: Path) -> None:
    """白名单之外的文件一律不读——不遍历主目录。"""
    decoy = fake_home / "secrets.json"
    decoy.write_text(json.dumps({"api_key": LITERAL_KEY}), encoding="utf-8")
    (fake_home / ".ssh").mkdir()
    (fake_home / ".ssh" / "id_rsa").write_text("PRIVATE KEY", encoding="utf-8")

    result = scan_local_agents()

    assert str(decoy) not in result.scanned_paths
    assert not any(".ssh" in p for p in result.scanned_paths)
    assert LITERAL_KEY not in result.model_dump_json()


def test_codex_auth_json_is_never_read(fake_home: Path) -> None:
    """auth.json 里是 OAuth token，属最敏感凭据，没有正当理由去碰。"""
    auth = fake_home / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True, exist_ok=True)
    auth.write_text(json.dumps({"OPENAI_API_KEY": LITERAL_KEY}), encoding="utf-8")
    _write_codex(fake_home, 'model = "gpt-5"\n')

    result = scan_local_agents()

    assert str(auth) not in result.scanned_paths
    assert LITERAL_KEY not in result.model_dump_json()


# ------------------------------------------------------------------ 各来源解析


def test_claude_code_picks_up_proxy_base_url(fake_home: Path) -> None:
    """很多用户走中转，base_url 必须被识别出来。"""
    _write_claude(
        fake_home,
        {
            "env": {
                "ANTHROPIC_BASE_URL": "https://relay.example.com",
                "ANTHROPIC_MODEL": "claude-sonnet-4-5",
                "ANTHROPIC_API_KEY": "${MY_RELAY_KEY}",
            }
        },
    )
    result = scan_local_agents()
    candidate = next(c for c in result.candidates if c.source == "claude_code")

    assert candidate.base_url == "https://relay.example.com"
    assert candidate.model == "claude-sonnet-4-5"
    assert candidate.adapter == "anthropic"
    # ${VAR} 形态应被识别成环境变量引用
    assert candidate.api_key_env == "MY_RELAY_KEY"
    assert resolve_api_key(candidate) == "${MY_RELAY_KEY}"


def test_codex_model_providers_parsed(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex 的 [model_providers.*] 显式声明了 base_url 与 env_key，是最理想的形态。"""
    monkeypatch.setenv("MY_OPENAI_KEY", LITERAL_KEY)
    _write_codex(
        fake_home,
        """
model = "gpt-5"
model_provider = "custom"

[model_providers.custom]
name = "My Gateway"
base_url = "https://gw.example.com/v1"
env_key = "MY_OPENAI_KEY"
""",
    )
    result = scan_local_agents()
    candidate = next(c for c in result.candidates if c.source == "codex")

    assert candidate.base_url == "https://gw.example.com/v1"
    assert candidate.model == "gpt-5"
    assert candidate.api_key_env == "MY_OPENAI_KEY"
    assert candidate.has_api_key is True
    assert LITERAL_KEY not in result.model_dump_json()


def test_codex_env_key_missing_is_flagged(fake_home: Path) -> None:
    """env_key 指向的变量没设置时要提示用户补，而不是假装能用。"""
    _write_codex(
        fake_home,
        """
[model_providers.custom]
base_url = "https://gw.example.com/v1"
env_key = "NOT_SET_ANYWHERE"
""",
    )
    result = scan_local_agents()
    candidate = next(c for c in result.candidates if c.source == "codex")

    assert candidate.has_api_key is False
    assert candidate.note and "NOT_SET_ANYWHERE" in candidate.note


def test_environment_variables_become_candidates(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """环境变量是最安全的来源，应直接产出候选。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", LITERAL_KEY)
    result = scan_local_agents()

    candidate = next(c for c in result.candidates if c.api_key_env == "DEEPSEEK_API_KEY")
    assert candidate.source == "environment"
    assert candidate.base_url == "https://api.deepseek.com/v1"
    assert candidate.model == "deepseek-chat"
    assert LITERAL_KEY not in result.model_dump_json()


def test_environment_base_url_override(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """走代理的用户常设 *_BASE_URL，应优先于默认地址。"""
    monkeypatch.setenv("OPENAI_API_KEY", LITERAL_KEY)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://my-proxy.example.com/v1")
    result = scan_local_agents()

    candidate = next(c for c in result.candidates if c.api_key_env == "OPENAI_API_KEY")
    assert candidate.base_url == "https://my-proxy.example.com/v1"


def test_already_imported_is_flagged(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """已导入过的候选要标出来，避免用户重复导入。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", LITERAL_KEY)
    result = scan_local_agents(existing_ids={"env-deepseek"})

    candidate = next(c for c in result.candidates if c.suggested_id == "env-deepseek")
    assert candidate.already_imported is True


# ------------------------------------------------------------------ 健壮性


def test_missing_configs_yield_empty_not_error(fake_home: Path) -> None:
    """什么都没装时应干净地返回空，而不是抛错。"""
    result = scan_local_agents()
    assert result.candidates == []
    assert result.scanned_paths == []
    assert result.errors == []


def test_malformed_config_is_skipped_gracefully(fake_home: Path) -> None:
    """配置文件损坏不能让整次扫描失败。"""
    path = fake_home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")
    _write_codex(fake_home, "this is not [valid toml")

    result = scan_local_agents()

    assert len(result.errors) >= 1
    assert all("无法解析" in e or "失败" in e for e in result.errors)


# ------------------------------------------------------------------ API 层


def test_scan_and_import_via_api(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """端到端：扫描 → 导入 → provider 落库为 ${VAR} 占位符（明文不落盘）。"""
    from fastapi.testclient import TestClient

    from agentmem.config import Settings
    from apps.api.main import create_app

    monkeypatch.setenv("DEEPSEEK_API_KEY", LITERAL_KEY)
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    models_yaml = tmp_path / "models.yaml"
    models_yaml.write_text("providers: []\nroles: {}\nfallbacks: {}\n", encoding="utf-8")
    settings = Settings(data_dir=data_dir, models_config=models_yaml)

    with TestClient(create_app(settings)) as client:
        scan = client.get("/api/v1/providers/local-agents")
        assert scan.status_code == 200
        body = scan.json()
        assert LITERAL_KEY not in scan.text, "扫描接口泄漏了明文密钥"
        target = next(c for c in body["candidates"] if c["api_key_env"] == "DEEPSEEK_API_KEY")

        created = client.post(
            "/api/v1/providers/import-local",
            json={"suggested_ids": [target["suggested_id"]]},
        )
        assert created.status_code == 201
        assert LITERAL_KEY not in created.text, "导入接口泄漏了明文密钥"

        # 落盘的必须是占位符，不是明文
        written = models_yaml.read_text(encoding="utf-8")
        assert LITERAL_KEY not in written, "明文密钥被写进了 models.yaml"
        assert "${DEEPSEEK_API_KEY}" in written

        # 重复导入应被拒绝（不覆盖已有配置）
        again = client.post(
            "/api/v1/providers/import-local",
            json={"suggested_ids": [target["suggested_id"]]},
        )
        assert again.status_code == 422
