"""配置加载：全局设置（环境变量）+ 模型 Provider 配置（YAML）。

``config/models.yaml`` 中以 ``${VAR}`` 形式引用环境变量，加载时统一插值；
未定义的环境变量替换为空串并记录告警，避免 API Key 缺失时整站启动失败。
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import structlog
import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from agentmem.types import (
    AgentMemModel,
    ProviderConfig,
    RoleBindings,
    RoleFallbacks,
    SpaceYaml,
)

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

logger = structlog.get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MODELS_CONFIG = PROJECT_ROOT / "config" / "models.yaml"


class Settings(BaseSettings):
    """全局设置，全部可由 ``AGENTMEM_`` 前缀的环境变量覆盖。"""

    model_config = SettingsConfigDict(
        env_prefix="AGENTMEM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    data_dir: Path = Field(default=Path("./data"), description="数据根目录")
    host: str = "127.0.0.1"
    port: int = 8765
    log_level: str = "INFO"
    env: str = Field(default="dev", description="dev | prod")
    models_config: Path = Field(default=DEFAULT_MODELS_CONFIG, description="模型配置路径")
    web_dist_dir: Path = Field(default=Path("./apps/web/dist"), description="前端产物目录")
    cors_origins: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173",
        description="逗号分隔的开发期跨域白名单",
    )
    device: str = Field(default="auto", description="本地模型设备：auto | cpu | cuda | mps")
    chunk_size: int = Field(default=512, ge=64, description="切片目标 token 数")
    chunk_overlap: int = Field(default=80, ge=0, description="切片重叠 token 数")
    embedding_batch_size: int = Field(default=32, ge=1)
    embedding_concurrency: int = Field(
        default=3,
        ge=1,
        le=16,
        description="向量化同时发出的批次数。向量化占了摄取绝大部分耗时，"
        "一批一批地等对远端 embedding 服务尤其浪费；写入仍是串行的",
    )
    warmup_on_start: bool = Field(
        default=True,
        description="启动后在后台预热中文分词词典，以及已绑定的本地向量 / 重排模型"
        "（含各 Space 的覆盖）。"
        "进程内模型要几十秒才就绪，放在第一次提问时加载会让首问一直转圈。"
        "环境变量 AGENTMEM_WARMUP_ON_START=false 可关闭（例如本地重排在 Metal 上不稳时）",
    )
    extract_concurrency: int = Field(
        default=3,
        ge=1,
        le=16,
        description="同时进行的抽取批次上限。一批要等一次模型调用（实测约 13 秒），"
        "串行跑一篇长文档要四分多钟；模型调用可以并发，落库仍串行",
    )
    ingest_concurrency: int = Field(
        default=2,
        ge=1,
        le=8,
        description="同时摄取的文档数。此前每投喂一篇就立刻起一个后台任务，"
        "一次投二十篇就是二十条流水线同时压着向量表与模型服务",
    )
    contextual_retrieval: bool = Field(
        default=True,
        description="摄取时给文档生成一段上下文，拼在被索引/嵌入的文本前面（不写进切片正文）",
    )

    @property
    def is_dev(self) -> bool:
        """是否为开发模式。"""
        return self.env.lower() != "prod"

    @property
    def cors_origin_list(self) -> list[str]:
        """跨域白名单列表。"""
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def spaces_dir(self) -> Path:
        """全部 Space 的根目录。"""
        return self.data_dir / "spaces"

    def ensure_dirs(self) -> None:
        """确保运行时数据目录存在。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.spaces_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取全局设置单例。"""
    return Settings()


class ModelsConfig(AgentMemModel):
    """``config/models.yaml`` 的完整结构。"""

    providers: list[ProviderConfig] = Field(default_factory=list)
    roles: RoleBindings = Field(default_factory=RoleBindings)
    fallbacks: RoleFallbacks = Field(default_factory=RoleFallbacks)


def read_env_file(path: Path) -> dict[str, str]:
    """读取 ``.env``；文件不存在时返回空表。

    只做 ``KEY=VALUE`` 的简单解析，支持 ``#`` 注释与成对引号。
    """
    if not path.is_file():
        return {}
    result: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        result[key] = value
    return result


def _interpolate(value: Any, env: dict[str, str], missing: list[str]) -> Any:
    """递归替换字符串中的 ``${VAR}`` 占位符。"""
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name in env:
                return env[name]
            missing.append(name)
            return ""

        return _ENV_PATTERN.sub(replace, value)
    if isinstance(value, dict):
        mapping = {key: _interpolate(item, env, missing) for key, item in value.items()}
        return mapping
    if isinstance(value, list):
        return [_interpolate(item, env, missing) for item in value]
    return value


def load_models_config(
    path: Path | None = None,
    *,
    env: dict[str, str] | None = None,
) -> ModelsConfig:
    """加载并解析模型配置。

    Args:
        path: YAML 路径，缺省用全局设置中的 ``models_config``。
        env: 插值用环境变量表，缺省为 ``.env`` 与进程环境变量的合并结果。

    Returns:
        解析后的配置；文件不存在时返回空配置。
    """
    settings = get_settings()
    config_path = path or settings.models_config
    if env is None:
        merged = read_env_file(PROJECT_ROOT / ".env")
        merged.update(os.environ)
        env = merged
    if not Path(config_path).is_file():
        return ModelsConfig()
    raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    missing: list[str] = []
    interpolated = _interpolate(raw, env, missing)
    if missing:
        logger.warning(
            "models_config_missing_env", variables=sorted(set(missing)), path=str(config_path)
        )
    if not isinstance(interpolated, dict):
        return ModelsConfig()
    config = ModelsConfig.model_validate(interpolated)
    _remember_api_key_sources(config, raw)
    return config


def _remember_api_key_sources(config: ModelsConfig, raw: Any) -> None:
    """记住每个 provider 的 ``api_key`` 在文件里的原始写法。

    这是保证写回时不泄密的**唯一可靠手段**：插值之后配置里只剩明文，
    单靠事后反查环境变量去猜哪个值原本是占位符既不可靠也不安全
    （环境变量可能已变更、多个变量可能取值相同）。
    """
    if not isinstance(raw, dict):
        return
    entries = raw.get("providers")
    if not isinstance(entries, list):
        return
    by_id = {
        entry["id"]: entry
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    }
    for provider in config.providers:
        original = by_id.get(provider.id, {}).get("api_key")
        if isinstance(original, str) and _ENV_PATTERN.search(original):
            provider.api_key_ref = original


#: 写回 ``models.yaml`` 时自动带上的说明头。
#:
#: 这个文件会被应用整体重写（在「设置 → 模型」里改任何东西都会触发），
#: PyYAML 的 dumper 不保留注释——实测一次「启用某个 provider」就抹掉了 125 行，
#: 包括文件头说明与各家云端 provider 的写法模板，而 README 正让用户去那里照抄。
#: 所以：**有价值的内容一律不放在这个文件里**，模板挪去 ``config/models.example.yaml``
#: （应用从不写它），这里只留一段每次重写都会重新生成的导航说明。
MODELS_YAML_HEADER = """\
# AgentMem 模型 Provider 配置
#
# ⚠️ 本文件由应用自动重写（在「设置 → 模型」里改动即触发），**手写的注释会丢失**。
#    需要参考各家云端 provider 的写法，见 config/models.example.yaml（应用从不改它）。
#
# - `api_key` 支持 `${ENV_VAR}` 占位符：加载时从环境变量 / .env 插值，
#   写回时自动还原成占位符，明文密钥不会落盘。
# - `adapter` 取值见 docs/01-ARCHITECTURE.md §4.3。
# - 适配器专属参数写在 `extra` 下（如 timeout、num_ctx），不影响其他适配器。
# - 角色绑定：业务代码只认角色名（chat/fast/distill/judge/embedding/rerank），
#   不认具体 provider id。

"""


def save_models_config(config: ModelsConfig, path: Path | None = None) -> None:
    """把模型配置写回 YAML，**不把明文密钥落盘**。

    还原 ``${VAR}`` 占位符的两级策略：

    1. 优先用加载时记下的原始写法（``api_key_ref``）—— 精确，无歧义。
    2. 没有来源记录时（例如用户刚在界面里手填的密钥），再退一步按当前环境变量
       反查；命中才还原。

    ⚠️ 反查**只作用于 ``api_key`` 字段**。早期实现对整棵配置树做反查，
    一旦某个环境变量的取值恰好等于某个 ``model`` 名或 ``base_url``，
    就会把它错误地改写成 ``${VAR}``，让配置在换台机器后直接失效。
    """
    settings = get_settings()
    config_path = Path(path or settings.models_config)
    env = read_env_file(PROJECT_ROOT / ".env")
    env.update(os.environ)
    reversed_env: dict[str, str] = {}
    for key, value in env.items():
        if value and key.isupper():
            reversed_env.setdefault(value, key)

    payload = config.model_dump(exclude_none=True, exclude_defaults=False)
    entries = payload.get("providers")
    if isinstance(entries, list):
        for entry, provider in zip(entries, config.providers, strict=False):
            entry.pop("api_key_ref", None)
            source = provider.api_key_ref
            if source is None and provider.api_key:
                name = reversed_env.get(provider.api_key)
                source = f"${{{name}}}" if name else None
            if source is not None:
                entry["api_key"] = source

    config_path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, default_flow_style=False)
    config_path.write_text(MODELS_YAML_HEADER + text, encoding="utf-8")


def load_space_yaml(path: Path) -> SpaceYaml:
    """读取 ``space.yaml``；不存在时返回默认配置。"""
    if not Path(path).is_file():
        return SpaceYaml()
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return SpaceYaml()
    return SpaceYaml.model_validate(raw)


def save_space_yaml(path: Path, config: SpaceYaml) -> None:
    """写回 ``space.yaml``。"""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    payload = config.model_dump(exclude_none=True)
    text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, default_flow_style=False)
    Path(path).write_text(text, encoding="utf-8")
