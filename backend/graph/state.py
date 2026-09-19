from typing import Any, Literal, TypedDict

Intent = Literal[
    "form_record",
    "natural_language_record",
    "view_progress",
    "generate_plan",
    "adjust_plan",
]

ConfirmationStatus = Literal["pending", "confirmed", "rejected"]

TerminationReason = Literal["safety_stop", "archive_draft", "reject_draft"]


class WorkflowState(TypedDict, total=False):
    conversation_id: str
    request: str
    intent: Intent | None
    context: Any
    loaded_skill: Any
    draft_plan_id: int | None
    draft_plan: Any
    evaluation: Any
    revision_count: int
    confirmation: ConfirmationStatus | None
    termination_reason: TerminationReason | None
