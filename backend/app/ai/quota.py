import anthropic
import openai


class ProviderQuotaExceeded(Exception):
    # 明确的供应商余额不足或账户配额耗尽：立即终止运行，独立于限流。
    pass


# 只接受真实供应商 SDK 错误：请求转换、usage 校验与工具参数校验错误不参与额度分类。
_SDK_ERRORS = (openai.OpenAIError, anthropic.AnthropicError)

# 供应商结构化账户额度码：命中即明确余额不足或配额耗尽，与限流码 rate_limit_exceeded 区分。
_QUOTA_CODES = frozenset({"insufficient_quota", "insufficient_credits"})

# 明确账户额度耗尽短语：必须整体表达余额不足或账户配额耗尽。
# 禁止凭 quota、billing、limit 等单个宽泛词分类：普通限流与参数错误不得命中。
_QUOTA_PHRASES = (
    "insufficient_quota",
    "insufficient quota",
    "insufficient credits",
    "insufficient credit",
    "insufficient balance",
    "insufficient funds",
    "credit balance is too low",
    "exceeded your current quota",
    "you have exceeded your quota",
    "quota exhausted",
    "quota has been exhausted",
    "billing hard limit",
    "余额不足",
    "余额已不足",
    "额度不足",
    "配额不足",
    "额度已耗尽",
    "配额已耗尽",
    "额度已用尽",
    "配额已用尽",
    "账户欠费",
)


def is_provider_quota_error(error: BaseException) -> bool:
    # 明确余额不足或账户配额耗尽：优先 SDK 解析的结构化 code/type，其次明确的账户额度短语。
    # 普通临时限流、鉴权、权限、上下文溢出、参数与网络错误保持原有语义。
    if not isinstance(error, _SDK_ERRORS):
        return False
    for value in (getattr(error, "code", None), getattr(error, "type", None)):
        if isinstance(value, str) and value.strip().lower() in _QUOTA_CODES:
            return True
    text = (getattr(error, "message", None) or str(error)).lower()
    return any(phrase in text for phrase in _QUOTA_PHRASES)


def raise_if_provider_quota_exceeded(error: BaseException) -> None:
    # 供应商错误边界共用：明确额度耗尽时抛出共享异常并保留原异常链，否则原样返回。
    if is_provider_quota_error(error):
        raise ProviderQuotaExceeded("模型供应商账户额度不足或配额耗尽") from error
