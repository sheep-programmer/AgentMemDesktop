"""统一异常体系。

所有 core 层异常均继承 :class:`AgentMemError`，携带稳定的 ``code`` 与语义化
``http_status``；``apps/api`` 的统一异常处理器把它们翻译成
``{"error": {"code", "message", "detail"}}`` 响应体。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ErrorBody(BaseModel):
    """错误体内的具体错误对象。"""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    """对外统一错误响应体。"""

    model_config = ConfigDict(extra="forbid")

    error: ErrorBody


class AgentMemError(Exception):
    """AgentMem 全部业务异常的基类。

    Args:
        message: 面向用户的可读信息。
        detail: 附加上下文，随响应体返回。
        code: 覆盖类默认错误码，通常不需要传。
        http_status: 覆盖类默认 HTTP 状态码，通常不需要传。
    """

    code: str = "INTERNAL_ERROR"
    http_status: int = 500

    def __init__(
        self,
        message: str,
        *,
        detail: dict[str, Any] | None = None,
        code: str | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.detail: dict[str, Any] = dict(detail or {})
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status

    def to_response(self) -> ErrorResponse:
        """转换为对外错误响应体。"""
        return ErrorResponse(
            error=ErrorBody(code=self.code, message=self.message, detail=self.detail)
        )

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


# ---------------------------------------------------------------------------
# `03-API-SPEC.md` §9 错误码表
# ---------------------------------------------------------------------------


class NotFoundError(AgentMemError):
    """资源不存在。"""

    code = "NOT_FOUND"
    http_status = 404

    def __init__(
        self,
        resource: str,
        identifier: str | None = None,
        *,
        detail: dict[str, Any] | None = None,
    ) -> None:
        message = f"{resource} 不存在" if identifier is None else f"{resource} 不存在：{identifier}"
        merged = dict(detail or {})
        merged.setdefault("resource", resource)
        if identifier is not None:
            merged.setdefault("id", identifier)
        super().__init__(message, detail=merged)


class ValidationError(AgentMemError):
    """参数错误。"""

    code = "VALIDATION_ERROR"
    http_status = 422


class ForbiddenOriginError(AgentMemError):
    """请求来自不被信任的网页或主机名（跨站请求 / DNS 重绑定）。"""

    code = "FORBIDDEN_ORIGIN"
    http_status = 403


class SpaceLockedError(AgentMemError):
    """该 Space 正在重建索引，暂不接受写操作。"""

    code = "SPACE_LOCKED"
    http_status = 409


class ProviderNotConfiguredError(AgentMemError):
    """角色未绑定 provider。"""

    code = "PROVIDER_NOT_CONFIGURED"
    http_status = 400

    def __init__(self, role: str, *, detail: dict[str, Any] | None = None) -> None:
        merged = dict(detail or {})
        merged.setdefault("role", role)
        super().__init__(f"角色 {role} 未绑定 provider", detail=merged)


class ProviderUnavailableError(AgentMemError):
    """连不上模型服务。"""

    code = "PROVIDER_UNAVAILABLE"
    http_status = 503


class ProviderTimeoutError(AgentMemError):
    """模型服务响应超时。"""

    code = "PROVIDER_TIMEOUT"
    http_status = 504


class ContextOverflowError(AgentMemError):
    """请求超出了模型的上下文窗口。

    单独成类是因为它有**可自动恢复**的处置方式：把对话历史裁短再试一次。
    混在 ``PROVIDER_UNAVAILABLE`` 里就只能整轮失败——而这条路径迟早会走到：
    答案提示词里放的是**全部**历史（为了让前缀只增不改、吃满供应商的前缀缓存，
    见 ``prompts/answer.py``），会话够长时必然撞上窗口上限。
    """

    code = "CONTEXT_OVERFLOW"
    http_status = 413


class ProviderInUseError(AgentMemError):
    """删除仍被角色引用的 provider。"""

    code = "PROVIDER_IN_USE"
    http_status = 409

    def __init__(self, provider_id: str, roles: list[str], **kw: Any) -> None:
        detail = dict(kw.pop("detail", None) or {})
        detail.setdefault("provider_id", provider_id)
        detail.setdefault("roles", roles)
        super().__init__(
            f"provider {provider_id} 仍被角色引用：{', '.join(roles)}",
            detail=detail,
            **kw,
        )


class EmbeddingDimMismatchError(AgentMemError):
    """向量维度与既有索引不符，必须重建索引。"""

    code = "EMBEDDING_DIM_MISMATCH"
    http_status = 409

    def __init__(
        self,
        expected: int,
        actual: int,
        *,
        table: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        merged = dict(detail or {})
        merged.setdefault("expected_dim", expected)
        merged.setdefault("actual_dim", actual)
        if table is not None:
            merged.setdefault("table", table)
        super().__init__(
            f"向量维度不匹配：索引为 {expected} 维，当前模型输出 {actual} 维，需要重建索引",
            detail=merged,
        )


class ParseFailedError(AgentMemError):
    """文档解析失败。"""

    code = "PARSE_FAILED"
    http_status = 422


class DuplicateDocumentError(AgentMemError):
    """sha256 已存在。"""

    code = "DUPLICATE_DOCUMENT"
    http_status = 409

    def __init__(
        self,
        sha256: str,
        *,
        document_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        merged = dict(detail or {})
        merged.setdefault("sha256", sha256)
        if document_id is not None:
            merged.setdefault("document_id", document_id)
        super().__init__("该文档已存在（sha256 重复）", detail=merged)


class EvalSetEmptyError(AgentMemError):
    """无测验题，无法评测。"""

    code = "EVAL_SET_EMPTY"
    http_status = 400


class InternalError(AgentMemError):
    """未预期的内部错误。"""

    code = "INTERNAL_ERROR"
    http_status = 500


#: 错误码 → 异常类，供需要按码构造异常的场景使用
ERROR_CLASSES: dict[str, type[AgentMemError]] = {
    cls.code: cls
    for cls in (
        NotFoundError,
        ValidationError,
        SpaceLockedError,
        ProviderNotConfiguredError,
        ProviderUnavailableError,
        ProviderTimeoutError,
        ProviderInUseError,
        EmbeddingDimMismatchError,
        ParseFailedError,
        DuplicateDocumentError,
        EvalSetEmptyError,
        InternalError,
    )
}
