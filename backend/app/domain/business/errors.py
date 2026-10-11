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
