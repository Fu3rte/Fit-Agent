from collections.abc import Sequence

from app.domain.business.models import BusinessFieldError


class BusinessError(Exception):
    code = "invalid_business_payload"
    http_status = 422
    message = "业务内容校验失败。"

    def __init__(
        self,
        message: str | None = None,
        *,
        errors: Sequence[BusinessFieldError] = (),
    ) -> None:
        self.errors = list(errors)
        super().__init__(message or self.message)

    def detail(self) -> dict:
        payload: dict = {"code": self.code, "message": str(self)}
        if self.code == "invalid_business_payload":
            payload["errors"] = [error.model_dump() for error in self.errors]
        return payload


class CommitVetoed(Exception):
    """本次提交在 COMMIT 语句执行瞬间被会话内容替换否决，写入已随事务回滚。"""


class InvalidBusinessPayload(BusinessError):
    code = "invalid_business_payload"
    http_status = 422
    message = "业务内容校验失败。"

    def __init__(self, errors: Sequence[BusinessFieldError]) -> None:
        super().__init__(errors=errors)


# 待确认提案读取命中已保存快照：三类业务共用，提示查询原保存操作状态。
class ProposalAlreadySaved(BusinessError):
    code = "proposal_already_saved"
    http_status = 409
    message = "该提案已保存，无法作为待确认内容读取。请查询原保存操作状态。"


class ProposalCancellationInvalid(BusinessError):
    code = "proposal_cancellation_invalid"
    http_status = 409
    message = "取消请求的用户节点、工具调用或展示时序不合法。"


# 画像自然语言确认与保存契约 §9 的七项业务错误码。

class ProfileProposalNotFound(BusinessError):
    code = "profile_proposal_not_found"
    http_status = 404
    message = "画像快照不存在。"


class ProfileProposalInvalidated(BusinessError):
    code = "profile_proposal_invalidated"
    http_status = 409
    message = "该画像快照已失效。"


class ProfileUpdateProcessing(BusinessError):
    code = "profile_update_processing"
    http_status = 409
    message = "该画像快照正在保存，请查询原操作状态。"


class ProfileVersionConflict(BusinessError):
    code = "profile_version_conflict"
    http_status = 409
    message = "画像版本已变化，需要重新整理并展示完整画像。"


class ProfileConfirmationInvalid(BusinessError):
    code = "profile_confirmation_invalid"
    http_status = 409
    message = "展示与确认消息的关联、角色、时序或有效路径不满足要求。"


class ProfileAccessDenied(BusinessError):
    code = "profile_access_denied"
    http_status = 403
    message = "画像快照归属校验失败。"


class WorkoutNotFound(BusinessError):
    code = "workout_not_found"
    http_status = 404
    message = "训练记录不存在。"


class WorkoutProposalNotFound(BusinessError):
    code = "workout_proposal_not_found"
    http_status = 404
    message = "训练记录快照不存在。"


class WorkoutProposalInvalidated(BusinessError):
    code = "workout_proposal_invalidated"
    http_status = 409
    message = "该训练记录快照已失效。"


class WorkoutSaveProcessing(BusinessError):
    code = "workout_save_processing"
    http_status = 409
    message = "该训练记录快照正在保存，请查询原操作状态。"


class WorkoutVersionConflict(BusinessError):
    code = "workout_version_conflict"
    http_status = 409
    message = "训练记录版本已变化，需要重新整理并展示完整记录。"


class WorkoutConfirmationInvalid(BusinessError):
    code = "workout_confirmation_invalid"
    http_status = 409
    message = "展示与确认消息的关联、角色、时序或有效路径不满足要求。"


class WorkoutAccessDenied(BusinessError):
    code = "workout_access_denied"
    http_status = 403
    message = "训练记录快照归属校验失败。"


class PlanNotFound(BusinessError):
    code = "plan_not_found"
    http_status = 404
    message = "计划不存在。"


class PlanProposalNotFound(BusinessError):
    code = "plan_proposal_not_found"
    http_status = 404
    message = "计划快照不存在。"


class PlanProposalInvalidated(BusinessError):
    code = "plan_proposal_invalidated"
    http_status = 409
    message = "该计划快照已失效。"


class PlanSaveProcessing(BusinessError):
    code = "plan_save_processing"
    http_status = 409
    message = "该计划快照正在保存，请查询原操作状态。"


class PlanVersionConflict(BusinessError):
    code = "plan_version_conflict"
    http_status = 409
    message = "当前计划已变化，需要重新准备并展示完整计划。"


class ProfileRequired(BusinessError):
    code = "profile_required"
    http_status = 409
    message = "生成计划需要已保存画像。"


class PlanConfirmationInvalid(BusinessError):
    code = "plan_confirmation_invalid"
    http_status = 409
    message = "展示与确认消息的关联、角色、时序或有效路径不满足要求。"


class PlanAccessDenied(BusinessError):
    code = "plan_access_denied"
    http_status = 403
    message = "计划快照归属校验失败。"


class ProfileSessionNotFound(BusinessError):
    code = "session_not_found"
    http_status = 404
    message = "当前会话不存在。"


# 结构化保存确认操作账本的错误码（方案 §7.4）。

class ConfirmationNotFound(BusinessError):
    code = "confirmation_not_found"
    http_status = 404
    message = "确认操作不存在。"


class ConfirmationOperationConflict(BusinessError):
    code = "operation_conflict"
    http_status = 409
    message = "操作请求冲突。"

    def __init__(self, message: str | None = None, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason

    def detail(self) -> dict:
        payload = super().detail()
        # 方案 §7.4：原操作因内容编辑失效时携带 reason=operation_expired 供确认 HTTP 表达。
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload


class ConfirmationBusy(BusinessError):
    code = "run_busy"
    http_status = 409
    message = "已有任务正在执行。"


class ConfirmationInterrupted(BusinessError):
    code = "confirmation_interrupted"
    http_status = 409
    message = "确认操作已中断。"


class ConfirmationSessionNotFound(BusinessError):
    code = "session_not_found"
    http_status = 404
    message = "当前会话不存在。"


# 动作检索工具的错误码：字段/器械/ID 求值失败、任务限制冲突与详情读取失败。

class InvalidSearchCondition(BusinessError):
    code = "invalid_search_condition"
    http_status = 422
    message = "检索条件无法映射到动作目录。"


class EquipmentRestrictionConflict(BusinessError):
    code = "equipment_restriction_conflict"
    http_status = 409
    message = "本次可用器械声明与已保存或临时不可用限制冲突，需要澄清并确认画像更新。"


class ExerciseNotFound(BusinessError):
    code = "exercise_not_found"
    http_status = 404
    message = "动作 ID 不存在于动作目录。"


class ExerciseDetailsTooLarge(BusinessError):
    code = "exercise_details_too_large"
    http_status = 422
    message = "单条完整动作详情超出响应预算。"


# 确认账本记录的失败操作只保留 code/message/errors，HTTP 状态沿用各业务错误自身定义。
# 遍历 BusinessError 子类按 code 建表，同码状态一致（如 session_not_found、invalid_business_payload）。
def _build_status_by_code() -> dict[str, int]:
    table: dict[str, int] = {}
    seen: set[int] = set()
    stack = [BusinessError]
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        code = getattr(current, "code", None)
        status = getattr(current, "http_status", None)
        if code and status:
            table.setdefault(code, status)
        stack.extend(current.__subclasses__())
    return table


_STATUS_BY_CODE = _build_status_by_code()


def business_http_status(code: str) -> int:
    return _STATUS_BY_CODE[code]
