"""复用本机已有的 AI Agent 配置（Claude Code / Codex / Continue / 环境变量）。

用户机器上往往已经装了 Claude Code、Codex 这类工具，密钥与 endpoint 都配好了。
与其让他再手填一遍，不如直接读出来一键导入。

## 安全设计（这是本模块最重要的部分）

本模块读取的是**用户的凭据文件**，因此每一条都必须守住：

1. **白名单路径**：只读下面 ``SOURCES`` 里写死的几个文件，**绝不遍历用户主目录**。
2. **只在用户主动触发时运行**：不在启动时、不在后台自动扫描。
3. **明文密钥不出本进程**：
   - 配置里若写的是「密钥在某个环境变量里」→ 导入成 ``${VAR}`` 占位符，
     复用 AgentMem 既有机制（写回 models.yaml 时仍是占位符，明文永不落盘）；
   - 配置里若是字面量密钥 → 对外只给**掩码**，真实值仅在导入那一刻取用。
   - :class:`LocalAgentCandidate` 结构上就没有放明文密钥的字段。
4. **不记录密钥**：日志只记路径与来源，绝不记值。
5. **透明**：响应里返回 ``scanned_paths``，用户能看到到底读了哪些文件。

## 为什么优先用环境变量占位符

``ANTHROPIC_API_KEY`` 这类变量本来就在用户 shell 里，导入成 ``${ANTHROPIC_API_KEY}``
既能用，又不会把密钥复制进 AgentMem 的配置文件——少一份明文副本就少一分泄漏面。
"""

from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import structlog

from agentmem.types import (
    LocalAgentCandidate,
    LocalAgentScanResponse,
    ProviderKind,
)

logger = structlog.get_logger(__name__)


def _mask(secret: str) -> str:
    """密钥掩码：只保留尾部 4 位。过短的整体打码，避免反推。"""
    if len(secret) <= 8:
        return "••••"
    return f"••••{secret[-4:]}"


def _home() -> Path:
    return Path.home()


# --------------------------------------------------------------------------
# 白名单路径：只读这些，绝不扫描整个主目录
# --------------------------------------------------------------------------


def _claude_paths() -> list[Path]:
    return [_home() / ".claude" / "settings.json", _home() / ".claude.json"]


def _codex_paths() -> list[Path]:
    return [_home() / ".codex" / "config.toml", _home() / ".config" / "codex" / "config.toml"]


def _continue_paths() -> list[Path]:
    return [_home() / ".continue" / "config.json"]


# --------------------------------------------------------------------------
# 环境变量：最安全的来源，直接转成 ${VAR} 占位符
# --------------------------------------------------------------------------

#: (环境变量名, kind, adapter, 默认 base_url, 默认 model, 展示名)
ENV_PROVIDERS: list[tuple[str, ProviderKind, str, str | None, str | None, str]] = [
    ("ANTHROPIC_API_KEY", "llm", "anthropic", "https://api.anthropic.com", None, "Anthropic"),
    ("OPENAI_API_KEY", "llm", "openai_compatible", "https://api.openai.com/v1", None, "OpenAI"),
    (
        "DEEPSEEK_API_KEY",
        "llm",
        "openai_compatible",
        "https://api.deepseek.com/v1",
        "deepseek-chat",
        "DeepSeek",
    ),
    (
        "DASHSCOPE_API_KEY",
        "llm",
        "openai_compatible",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "qwen-plus",
        "通义千问",
    ),
    (
        "ZHIPUAI_API_KEY",
        "llm",
        "openai_compatible",
        "https://open.bigmodel.cn/api/paas/v4",
        "glm-4-plus",
        "智谱 GLM",
    ),
    (
        "MOONSHOT_API_KEY",
        "llm",
        "openai_compatible",
        "https://api.moonshot.cn/v1",
        "moonshot-v1-8k",
        "Moonshot",
    ),
    (
        "SILICONFLOW_API_KEY",
        "rerank",
        "cohere_rerank",
        "https://api.siliconflow.cn/v1",
        "BAAI/bge-reranker-v2-m3",
        "硅基流动 Rerank",
    ),
]


def _scan_environment() -> Iterator[LocalAgentCandidate]:
    """从环境变量识别可用的 provider。密钥一律转成 ``${VAR}`` 占位符。"""
    for env_name, kind, adapter, base_url, model, label in ENV_PROVIDERS:
        value = os.environ.get(env_name)
        if not value or not value.strip():
            continue
        # base_url 允许被同名的 *_BASE_URL 覆盖（很多用户走代理）
        override = os.environ.get(env_name.replace("_API_KEY", "_BASE_URL"))
        yield LocalAgentCandidate(
            source="environment",
            source_label=f"环境变量 · {label}",
            source_path=None,
            suggested_id=f"env-{env_name.lower().replace('_api_key', '').replace('_', '-')}",
            kind=kind,
            adapter=adapter,
            model=model,
            base_url=(override or base_url),
            has_api_key=True,
            api_key_env=env_name,
            api_key_hint=None,
            note=f"密钥将以 ${{{env_name}}} 占位符导入，明文不会写入配置文件",
        )


# --------------------------------------------------------------------------
# Claude Code
# --------------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.info("local_agent_config_unreadable", path=str(path), error=type(exc).__name__)
        return None
    return data if isinstance(data, dict) else None


def _scan_claude_code(scanned: list[str], errors: list[str]) -> Iterator[LocalAgentCandidate]:
    """读取 Claude Code 的 settings。

    关心的是它的 ``env`` 块——很多用户在这里配 ``ANTHROPIC_BASE_URL``（走代理/中转）
    与 ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_AUTH_TOKEN``。
    """
    for path in _claude_paths():
        if not path.is_file():
            continue
        scanned.append(str(path))
        data = _read_json(path)
        if data is None:
            errors.append(f"{path} 无法解析（已跳过）")
            continue

        env = data.get("env")
        env = env if isinstance(env, dict) else {}
        base_url = env.get("ANTHROPIC_BASE_URL") or data.get("ANTHROPIC_BASE_URL")
        model = env.get("ANTHROPIC_MODEL") or data.get("model")
        raw_key = env.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN")

        # settings 里没写任何可用信息就不产出候选，避免给用户一个空壳
        if not (base_url or model or raw_key):
            continue

        # 判断密钥形态：${VAR} 占位符 / 字面量 / 缺失
        api_key_env, hint, note = _classify_secret(raw_key, fallback_env="ANTHROPIC_API_KEY")
        yield LocalAgentCandidate(
            source="claude_code",
            source_label="Claude Code",
            source_path=str(path),
            suggested_id="claude-code",
            kind="llm",
            adapter="anthropic",
            model=model if isinstance(model, str) else None,
            base_url=base_url if isinstance(base_url, str) else "https://api.anthropic.com",
            has_api_key=bool(api_key_env or hint),
            api_key_env=api_key_env,
            api_key_hint=hint,
            note=note,
        )
        return  # 两个路径取先命中的那个，不重复产出


def _classify_secret(
    raw: object, *, fallback_env: str | None = None
) -> tuple[str | None, str | None, str | None]:
    """把配置里读到的密钥归类成 (环境变量名, 掩码, 提示)。

    三种形态：
    - ``${VAR}`` 或 ``$VAR`` 占位符 → 取变量名，导入时原样保留占位符（最理想）
    - 字面量密钥 → 只回掩码；若同名环境变量恰好存在同一个值，优先用环境变量引用
    - 缺失 → 提示用户导入后手动补
    """
    if not isinstance(raw, str) or not raw.strip():
        if fallback_env and os.environ.get(fallback_env):
            return fallback_env, None, f"密钥取自环境变量 {fallback_env}"
        return None, None, "该配置未包含密钥，导入后需手动填写"

    value = raw.strip()
    if value.startswith("${") and value.endswith("}"):
        return value[2:-1], None, None
    if value.startswith("$") and len(value) > 1:
        return value[1:], None, None

    # 字面量：若某个已知环境变量存的就是同一个值，改用引用，避免再复制一份明文
    for env_name in (fallback_env, *(name for name, *_ in ENV_PROVIDERS)):
        if env_name and os.environ.get(env_name) == value:
            return env_name, None, f"已识别为环境变量 {env_name}，将以占位符导入"
    return None, _mask(value), "配置文件中为明文密钥，导入后建议改用环境变量占位符"


# --------------------------------------------------------------------------
# Codex
# --------------------------------------------------------------------------


def _scan_codex(scanned: list[str], errors: list[str]) -> Iterator[LocalAgentCandidate]:
    """读取 Codex 的 ``config.toml``。

    关心 ``[model_providers.*]``：它显式写了 ``base_url`` 与 ``env_key``
    （密钥所在的环境变量名）——这正是我们最想要的形态，无需接触任何明文。

    ⚠️ **不读 ``auth.json``**：那里面是 OAuth token，不是可复用的 API key，
    且属于最敏感的凭据，没有正当理由去碰它。
    """
    for path in _codex_paths():
        if not path.is_file():
            continue
        scanned.append(str(path))
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            errors.append(f"{path} 无法解析（已跳过）")
            logger.info("codex_config_unreadable", path=str(path), error=type(exc).__name__)
            continue

        default_model = data.get("model") if isinstance(data.get("model"), str) else None
        providers = data.get("model_providers")
        if not isinstance(providers, dict):
            continue

        for name, entry in providers.items():
            if not isinstance(entry, dict):
                continue
            base_url = entry.get("base_url")
            env_key = entry.get("env_key")
            if not isinstance(base_url, str):
                continue
            yield LocalAgentCandidate(
                source="codex",
                source_label=f"Codex · {entry.get('name') or name}",
                source_path=str(path),
                suggested_id=f"codex-{str(name).lower().replace('_', '-')}",
                kind="llm",
                adapter="openai_compatible",
                model=default_model,
                base_url=base_url,
                has_api_key=bool(env_key and os.environ.get(str(env_key))),
                api_key_env=str(env_key) if isinstance(env_key, str) else None,
                api_key_hint=None,
                note=None
                if (env_key and os.environ.get(str(env_key)))
                else f"密钥取自环境变量 {env_key}，当前未设置，导入后需补全"
                if env_key
                else "该 provider 未声明密钥来源，导入后需手动填写",
            )
        return


# --------------------------------------------------------------------------
# Continue（VS Code / JetBrains 插件）
# --------------------------------------------------------------------------


def _scan_continue(scanned: list[str], errors: list[str]) -> Iterator[LocalAgentCandidate]:
    """读取 Continue 的 ``config.json``，取其 ``models`` 数组。"""
    for path in _continue_paths():
        if not path.is_file():
            continue
        scanned.append(str(path))
        data = _read_json(path)
        if data is None:
            errors.append(f"{path} 无法解析（已跳过）")
            continue
        models = data.get("models")
        if not isinstance(models, list):
            continue
        for index, entry in enumerate(models):
            if not isinstance(entry, dict):
                continue
            model = entry.get("model")
            if not isinstance(model, str):
                continue
            api_key_env, hint, note = _classify_secret(entry.get("apiKey"))
            title = entry.get("title") or entry.get("provider") or model
            yield LocalAgentCandidate(
                source="continue",
                source_label=f"Continue · {title}",
                source_path=str(path),
                suggested_id=f"continue-{str(model).lower().replace('/', '-').replace(':', '-')}"[
                    :48
                ]
                or f"continue-{index}",
                kind="llm",
                adapter="openai_compatible",
                model=model,
                base_url=entry.get("apiBase") if isinstance(entry.get("apiBase"), str) else None,
                has_api_key=bool(api_key_env or hint),
                api_key_env=api_key_env,
                api_key_hint=hint,
                note=note,
            )
        return


# --------------------------------------------------------------------------
# 对外入口
# --------------------------------------------------------------------------


def scan_local_agents(existing_ids: set[str] | None = None) -> LocalAgentScanResponse:
    """扫描本机已有的 Agent 配置，返回可导入的 provider 候选。

    纯同步文件读取（都是小文件），调用方在 ``asyncio.to_thread`` 里跑即可。

    Args:
        existing_ids: 已存在的 provider id，用于标记 ``already_imported``。
    """
    scanned: list[str] = []
    errors: list[str] = []
    candidates: list[LocalAgentCandidate] = []

    for scanner in (_scan_claude_code, _scan_codex, _scan_continue):
        try:
            candidates.extend(scanner(scanned, errors))
        except Exception as exc:  # 单个来源失败不该让整次扫描失败
            errors.append(f"{scanner.__name__} 扫描失败：{type(exc).__name__}")
            logger.warning("local_agent_scan_failed", scanner=scanner.__name__)

    candidates.extend(_scan_environment())

    known = existing_ids or set()
    for candidate in candidates:
        candidate.already_imported = candidate.suggested_id in known

    logger.info(
        "local_agent_scan_done",
        found=len(candidates),
        scanned=len(scanned),
        errors=len(errors),
    )
    return LocalAgentScanResponse(candidates=candidates, scanned_paths=scanned, errors=errors)


def resolve_api_key(candidate: LocalAgentCandidate) -> str | None:
    """导入时取出该候选要写入的 ``api_key`` 值。

    - 有 ``api_key_env`` → 返回 ``${VAR}`` **占位符字符串**（不是明文！），
      交给 config 层原样写进 models.yaml；
    - 否则 → 返回 ``None``，由用户在界面上手动补。

    **刻意不从配置文件里回捞明文密钥**：候选结构里本来就没存它，
    要用明文只能让用户自己填一次——少一条明文复制路径。
    """
    if candidate.api_key_env:
        return f"${{{candidate.api_key_env}}}"
    return None
