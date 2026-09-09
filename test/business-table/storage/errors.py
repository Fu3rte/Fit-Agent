"""业务存储层错误。机器错误码与 architecture-decisions.md 对齐。"""


class BusinessError(Exception):
    http_status = 400
    error_code = "business_error"


class ValidationError(BusinessError):
    http_status = 422
    error_code = "validation_error"


class DraftStale(BusinessError):
    http_status = 409
    error_code = "draft_stale"


class ScheduleLocked(BusinessError):
    http_status = 409
    error_code = "schedule_locked"
