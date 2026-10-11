import anthropic
import openai

from app.ai.messages import AssistantMessage, Usage

ZERO_USAGE = Usage(
    input=0,
    output=0,
    cache_read=0,
    cache_write=0,
    total_tokens=0,
)

_CAPACITY_STATUS = frozenset({400, 413, 422})

# 排除规则优先于容量文本匹配：限流、鉴权、权限、资源缺失、连接、超时与普通服务错误。
_EXCLUDED_ERRORS = (
    openai.RateLimitError,
    openai.AuthenticationError,
    openai.PermissionDeniedError,
    openai.NotFoundError,
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.InternalServerError,
    anthropic.RateLimitError,
    anthropic.AuthenticationError,
    anthropic.PermissionDeniedError,
    anthropic.NotFoundError,
    anthropic.APIConnectionError,
    anthropic.APITimeoutError,
    anthropic.InternalServerError,
)

# 供应商结构化容量码：命中即明确溢出。
_OVERFLOW_CODES = frozenset({"context_length_exceeded"})

# 明确容量短语：本身已表达输入或请求超过模型上下文容量。
_OVERFLOW_PHRASES = (
    "exceed context limit",
    "exceeds the context window",
    "exceeds context window",
    "exceeds the maximum number of tokens",
    "prompt is too long",
    "input is too long",
    "reduce the length",
)

# 组合规则：上下文容量名词加超出或过量信号，避免仅凭名词误判参数校验。
_CONTEXT_TERMS = ("context length", "context window", "context limit")
_EXCEED_TERMS = (
    "exceed",
    "too long",
    "too many tokens",
    "resulted in",
    "maximum number of tokens",
)


class ContextOverflowError(Exception):
    # 明确上下文溢出：恢复后再次溢出时作为运行终态候选抛出。
    pass


def _capacity_text(text: str) -> bool:
    if any(phrase in text for phrase in _OVERFLOW_PHRASES):
        return True
    return any(term in text for term in _CONTEXT_TERMS) and any(
        term in text for term in _EXCEED_TERMS
    )


def _structured_code(error: BaseException) -> str | None:
    # 供应商结构化错误体由 SDK 解析：优先顶层 code，其次 body.error.code。
    code = getattr(error, "code", None)
    if isinstance(code, str):
        return code
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        inner = body.get("error")
        if isinstance(inner, dict) and isinstance(inner.get("code"), str):
            return inner["code"]
    return None


def is_context_overflow_error(error: BaseException) -> bool:
    # 只接受明确的 400/413/422 容量错误；限流、鉴权、连接、超时及普通错误排除。
    if isinstance(error, _EXCLUDED_ERRORS):
        return False
    if getattr(error, "status_code", None) not in _CAPACITY_STATUS:
        return False
    if _structured_code(error) in _OVERFLOW_CODES:
        return True
    text = (getattr(error, "message", None) or str(error)).lower()
    return _capacity_text(text)


def build_overflow_message(
    output: AssistantMessage, error: BaseException
) -> AssistantMessage:
    # 失败助手消息：保留已有内容与模型身份，usage 缺省时记零值，错误信息保留真实文本。
    values = output.model_dump(exclude_unset=True)
    values["content"] = output.content
    values["usage"] = output.usage if output.usage is not None else ZERO_USAGE
    values["stop_reason"] = "error"
    values["error_message"] = getattr(error, "message", None) or str(error)
    values["context_overflow"] = True
    return AssistantMessage.model_validate(values)
