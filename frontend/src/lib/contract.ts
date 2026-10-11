/* ===== 会话持久化请求层（backend-http-sse-contract §11）===== */

/** 会话对象（§11.2）：``active_leaf_id`` 为 UUID 或 null，其余字段非空 */
export interface SessionWire {
  session_id: string;
  title: string;
  active_leaf_id: string | null;
  created_at: number;
  updated_at: number;
}

/** 运行状态（§11.2） */
export type RunStatusWire =
  "running" | "completed" | "failed" | "cancelled" | "interrupted";

/** 运行对象（§11.2）：``last_entry_id``／``finished_at`` 与错误字段允许 null */
export interface SessionRunWire {
  session_id: string;
  run_id: string;
  request_entry_id: string;
  last_entry_id: string | null;
  status: RunStatusWire;
  started_at: number;
  finished_at: number | null;
  error_code: string | null;
  error_message: string | null;
}

/** 输入状态（§11.2） */
export type SteeringStatusWire =
  "pending" | "consumed" | "withdrawn" | "discarded";

/** 丢弃原因（§11.2） */
export type DiscardReasonWire =
  "completed" | "failed" | "cancelled" | "interrupted";

/** 输入对象（§11.2）：``entry_id`` 仅 consumed 有值，``reason`` 仅 discarded 有值，``attachments`` 按受理顺序 */
export interface SteeringInputWire {
  session_id: string;
  run_id: string;
  steering_id: string;
  status: SteeringStatusWire;
  entry_id: string | null;
  reason: DiscardReasonWire | null;
  created_at: number;
  updated_at: number;
  attachments: AttachmentWire[];
}

/** 操作类型（§11.2） */
export type OperationKindWire = "send" | "edit" | "regenerate" | "steering";

/* ===== 附件 wire（plan-import-adjustment-contract §2）===== */

/** 新上传文件：``attachment_id`` 由前端生成并在显式重试时沿用，``data_base64`` 为原始字节的标准 Base64 */
export interface AttachmentUploadWire {
  kind: "upload";
  attachment_id: string;
  file_name: string;
  data_base64: string;
}

/** 引用同会话已保存附件：编辑保留项使用该输入 */
export interface AttachmentReferenceWire {
  kind: "reference";
  attachment_id: string;
}

export type AttachmentInputWire =
  AttachmentUploadWire | AttachmentReferenceWire;

/** 已受理附件的公开元数据（§4）：顺序来自节点关联顺序，响应不含存储引用 */
export interface AttachmentWire {
  attachment_id: string;
  file_name: string;
  size_bytes: number;
  created_at: number;
}

/** ``GET /api/sessions/{session_id}/attachments/{attachment_id}`` 响应（§4）：元数据加严格 UTF-8 解码正文 */
export interface AttachmentContentWire extends AttachmentWire {
  text: string;
}

/** 创建会话请求体（§4）：``session_id`` 标准 UUID，``title`` 为首条请求原文 */
export interface SessionCreateBody {
  session_id: string;
  title: string;
}

/** 发送请求体（§5）：``operation_id`` 为同一次操作的幂等键，显式重试沿用原值；新前端始终提交 ``attachments`` */
export interface ReActRunBody {
  session_id: string;
  operation_id: string;
  request: string;
  attachments: AttachmentInputWire[];
}

/** 编辑请求体：新操作提供完整附件集合；旧账本重试省略该字段，沿用目标原附件。 */
export interface EditRunBody {
  session_id: string;
  operation_id: string;
  target_entry_id: string;
  request: string;
  attachments?: AttachmentInputWire[];
}

/** 重新生成请求体（session-edit-regenerate-contract §4）：``target_entry_id`` 为执行起点的用户节点 */
export interface RegenerateRunBody {
  session_id: string;
  operation_id: string;
  target_entry_id: string;
}

/** 重复发送返回的 JSON（§11.3）：原运行的关联身份与当前持久化状态 */
export interface SendRunRepeatWire {
  operation_id: string;
  session_id: string;
  run_id: string;
  request_entry_id: string;
  status: RunStatusWire;
}

/** 接收 Steering 请求体（§6.1）：文字与附件一并受理，附件非空时允许空文字 */
export interface SteeringBody {
  session_id: string;
  operation_id: string;
  message: string;
  attachments: AttachmentInputWire[];
}

/** 接收 Steering 响应状态（§11.3）：首次为 accepted，重复为原输入当前持久化状态 */
export type SteeringReceiveStatusWire = "accepted" | SteeringStatusWire;

/** 接收 Steering 响应（§11.3） */
export interface SteeringReceiveWire {
  operation_id: string;
  session_id: string;
  run_id: string;
  steering_id: string;
  created: boolean;
  status: SteeringReceiveStatusWire;
  entry_id: string | null;
  reason: DiscardReasonWire | null;
}

/** 撤回 Steering 请求体（§6.2） */
export interface SteeringWithdrawBody {
  session_id: string;
}

/** 撤回 Steering 响应（§11.3）：``status`` 为 withdrawn 或已有 discarded */
export interface SteeringWithdrawWire {
  session_id: string;
  run_id: string;
  steering_id: string;
  status: "withdrawn" | "discarded";
  entry_id: string | null;
  reason: DiscardReasonWire | null;
}

/** 操作查询响应（§11.3）：未受理时 kind／run／steering 全为 null */
export interface OperationQueryWire {
  operation_id: string;
  session_id: string;
  accepted: boolean;
  kind: OperationKindWire | null;
  run: SessionRunWire | null;
  steering: SteeringInputWire | null;
}

/** SSE 响应头（§11.4）：四个身份均为标准 UUID 字符串 */
export interface AgentStreamHeaders {
  session_id: string;
  operation_id: string;
  run_id: string;
  request_entry_id: string;
}

/* ===== 会话列表与当前分支历史（session-history-contract §2、§3）===== */

/** GET /api/sessions：全量会话头（§2） */
export interface SessionListWire {
  sessions: SessionWire[];
}

/** DELETE /api/sessions/{session_id}（session-delete-contract §4.2）：``deleted`` 固定为 true */
export interface SessionDeleteWire {
  session_id: string;
  deleted: true;
}

/** 助手公开内容块（§3.2）：历史与 SSE 使用同一公开快照规则 */
export type PublicAssistantContent = ReActContent;

/** 历史消息公开投影（§3.2）：系统消息不含可见字段，前端不产生可见消息 */
export type PublicMessageWire =
  | { role: "system" }
  | {
      role: "user";
      text: string;
      timestamp: number;
      attachments: AttachmentWire[];
    }
  | {
      role: "assistant";
      content: PublicAssistantContent[];
      stop_reason: ReActStopReason;
      timestamp: number;
    }
  | {
      role: "toolResult";
      tool_call_id: string;
      tool_name: string;
      content: string;
      is_error: boolean;
      timestamp: number;
    };

/** 一个已提交节点（§3.1、compaction-contract-decisions §A）：entries 为 active_leaf_id 的祖先链，
 *  按根到叶排列；message 节点公开消息投影，compaction 为隐藏结构节点，摘要、usage、system_message
 *  及内部附件路径保留在后端，两种节点都参与祖先链、运行归属与叶节点校验 */
export type HistoryEntryWire =
  | {
      type: "message";
      entry_id: string;
      parent_id: string | null;
      run_id: string | null;
      created_at: number;
      message: PublicMessageWire;
    }
  | {
      type: "compaction";
      entry_id: string;
      parent_id: string | null;
      run_id: string | null;
      created_at: number;
    };

/** 历史运行对象（§3.3）：与运行接口契约的运行对象同形 */
export type HistoryRunWire = SessionRunWire;

/** 历史输入状态（§3.4）：consumed 关联用户节点，其余状态不伪造节点；``attachments`` 为受理时集合 */
export interface HistorySteeringWire {
  session_id: string;
  run_id: string;
  steering_id: string;
  text: string;
  attachments: AttachmentWire[];
  timestamp: number;
  status: SteeringStatusWire;
  entry_id: string | null;
  reason: DiscardReasonWire | null;
  created_at: number;
  updated_at: number;
}

/* ===== 画像查询（backend-http-sse-contract §11.9）===== */

/** 画像八个必填字段：文本字段 null 表示未知；限制列表 null 表示未知，[] 表示明确没有限制 */
export interface ProfileContentWire {
  goal: string | null;
  experience: string | null;
  environment: string | null;
  availability: string | null;
  health_notes: string | null;
  movement_restrictions: string | null;
  unavailable_equipment: string[] | null;
  forbidden_exercise_ids: string[] | null;
}

/** GET /api/profile：未建档时 ``version`` 与 ``content`` 同时为 null */
export interface ProfileResponseWire {
  version: number | null;
  content: ProfileContentWire | null;
}

/* ===== 画像自然语言确认与保存（backend-http-sse-contract §11.8、§11.9）===== */

/** 快照保存状态：仅 saved 携带完整保存结果 */
export type ProfileProposalStatusWire =
  "pending" | "processing" | "saved" | "invalidated" | "conflicted";

/** `prepare_profile_update` 输入：单用户目标画像、查询得到的依据版本与完整待确认内容 */
export interface ProfileProposalArgumentsWire {
  /** 目标画像：严格整数且固定 1（单用户本地画像 id） */
  profile_id: number;
  base_profile_version: number | null;
  payload: ProfileContentWire;
}

/** `prepare_profile_update` 输出：后端生成的快照标识与固定内容，前端完整展示 `payload` */
export interface ProfileProposalWire {
  proposal_id: string;
  profile_id: number;
  base_profile_version: number | null;
  payload: ProfileContentWire;
}

/** `save_profile_update` 输入：快照标识、完整画像展示节点与用户确认节点 */
export interface ProfileSaveArgumentsWire {
  proposal_id: string;
  display_entry_id: string;
  confirmation_entry_id: string;
}

/** `save_profile_update` 成功输出，同时是 saved 状态的固定保存结果与幂等记录内容 */
export interface ProfileSaveResultWire {
  proposal_id: string;
  profile_id: number;
  version: number;
  content: ProfileContentWire;
  /** UTC 毫秒；重复保存保持原值 */
  saved_at: number;
}

/** `get_profile_update_status` 输入 */
export interface ProfileStatusArgumentsWire {
  proposal_id: string;
}

/** `get_profile_update_status` 输出：`saved` 的 `result` 为完整固定结果，其他状态为 null */
export interface ProfileStatusResultWire {
  proposal_id: string;
  status: ProfileProposalStatusWire;
  result: ProfileSaveResultWire | null;
}

/* ===== 实际训练记录（workout-http-sse-contract §2、§3、§5）===== */

/** 训练记录实际采用的七种重量口径（与 domain/business/models.py 的 LoadConvention 同集合；目录口径 §11.7） */
export type WorkoutLoadConvention =
  | "per_implement"
  | "barbell_total"
  | "machine_display"
  | "plates_total"
  | "per_side"
  | "added_weight"
  | "assistance_weight";

/** 已知完成的一组：三个数值允许全部为 null，表示该组具体数据未知 */
export interface WorkoutSetWire {
  reps: number | null;
  weight_kg: number | null;
  duration_seconds: number | null;
}

/** 一个实际动作：``exercise_id`` 为已核实目录 ID，目录外动作为 null 并保留名称；``sets=[]`` 表示组数未知 */
export interface WorkoutExerciseWire {
  exercise_id: string | null;
  name: string;
  load_convention: WorkoutLoadConvention | null;
  sets: WorkoutSetWire[];
}

/** 完整训练内容：至少一个动作，``notes`` 为感受或 null */
export interface WorkoutContentWire {
  exercises: WorkoutExerciseWire[];
  notes: string | null;
}

/** 一条训练记录（§3）：更新保持 id，version 递增，时间戳为 UTC 毫秒 */
export interface WorkoutRecordWire {
  id: string;
  performed_on: string;
  version: number;
  content: WorkoutContentWire;
  created_at: number;
  updated_at: number;
}

/** 快照保存状态：仅 saved 携带完整保存结果 */
export type WorkoutProposalStatusWire =
  "pending" | "processing" | "saved" | "invalidated" | "conflicted";

/** `prepare_workout` 输出：新增时基础记录 ID 与版本同时为 null，``payload`` 为完整训练内容 */
export interface WorkoutProposalWire {
  proposal_id: string;
  performed_on: string;
  base_workout_id: string | null;
  base_workout_version: number | null;
  payload: WorkoutContentWire;
}

/** 固定保存结果，同时是幂等记录内容：重复提交返回原结果 */
export interface WorkoutSaveResultWire {
  proposal_id: string;
  id: string;
  performed_on: string;
  version: number;
  content: WorkoutContentWire;
  created_at: number;
  updated_at: number;
  saved_at: number;
}

/** `get_workout_save_status` 输出：`saved` 的 `result` 为完整固定结果，其他状态为 null */
export interface WorkoutStatusResultWire {
  proposal_id: string;
  status: WorkoutProposalStatusWire;
  result: WorkoutSaveResultWire | null;
}

/** GET /api/workouts 响应（workout-http-sse-contract §2）：items 为完整记录，按 performed_on、id 降序 */
export interface WorkoutListWire {
  items: WorkoutRecordWire[];
  page: number;
  page_size: number;
  total: number;
}

/* ===== 训练计划生成与采纳（plan-generation-contract §2、§4、§5）===== */

/** 计划重量口径与训练记录共用同一枚举集合（契约 §2 尾注） */
export type PlanLoadConvention = WorkoutLoadConvention;

/** 计划里的一个动作：目录外动作 ``exercise_id`` 为 null 并保留名称；数值 null 保持未知 */
export interface PlanExerciseWire {
  exercise_id: string | null;
  name: string;
  sets: number | null;
  reps: number | null;
  duration_seconds: number | null;
  weight_kg: number | null;
  load_convention: PlanLoadConvention | null;
  rest_seconds: number | null;
}

/** 一个训练日：``kind=rest`` 时 ``exercises`` 必须为空数组，通用结构允许训练日为空 */
export interface PlanDayWire {
  kind: "training" | "rest";
  focus: string | null;
  exercises: PlanExerciseWire[];
  notes: string | null;
}

/** 计划内容：``repeat=null`` 为循环方式未知，``days`` 顺序即训练日顺序 */
export interface PlanContentWire {
  repeat: boolean | null;
  days: PlanDayWire[];
  notes: string | null;
  /** JSON Pointer 字段路径，标记助手补充或修改的字段 */
  suggested_fields: string[];
}

/** GET /api/plans/current：没有当前计划时 ``id`` 与 ``content`` 同时为 null */
export type CurrentPlanWire =
  | { id: null; content: null }
  | { id: string; content: PlanContentWire };

/** 一个已保存计划版本（§4）：固定内容，``is_current`` 由实时查询给出 */
export interface PlanRecordWire {
  id: string;
  is_current: boolean;
  created_at: number;
  content: PlanContentWire;
}

/** GET /api/plans：直接数组，按 created_at 降序、同时间按 id 降序 */
export type PlanListWire = PlanRecordWire[];

/** `get_plan` 输入 */
export interface PlanGetArgumentsWire {
  plan_id: string;
}

/** `prepare_plan` 输入：画像已保存版本、查询得到的当前计划 ID 原值与完整内容 */
export interface PlanProposalArgumentsWire {
  base_profile_version: number;
  base_plan_id: string | null;
  payload: PlanContentWire;
}

/** `prepare_plan` 输出：快照标识、依据字段与完整 ``payload``，``display_entry_id`` 来自持久化节点 */
export interface PlanProposalWire extends PlanProposalArgumentsWire {
  proposal_id: string;
}

/** `prepare_plan_import` 与 `prepare_plan_adjustment` 共用输入（plan-import-adjustment-contract §7）：
 *  ``base_profile_version`` 允许 null 表示准备时没有画像 */
export interface PlanImportArgumentsWire {
  base_profile_version: number | null;
  base_plan_id: string | null;
  payload: PlanContentWire;
}

export type PlanAdjustmentArgumentsWire = PlanImportArgumentsWire;

/** `prepare_plan_import` 输出 */
export interface PlanImportProposalWire extends PlanImportArgumentsWire {
  proposal_id: string;
  preparation_kind: "import";
}

/** `prepare_plan_adjustment` 输出 */
export interface PlanAdjustmentProposalWire extends PlanAdjustmentArgumentsWire {
  proposal_id: string;
  preparation_kind: "adjustment";
}

/** 三类计划准备结果：录入与调整按实际工具校验，生成快照保持生成契约原值 */
export type PreparedPlanProposalWire =
  PlanProposalWire | PlanImportProposalWire | PlanAdjustmentProposalWire;

/** `save_plan` 输入：快照标识、完整展示节点与用户确认节点 */
export interface PlanSaveArgumentsWire {
  proposal_id: string;
  display_entry_id: string;
  confirmation_entry_id: string;
}

/** `save_plan` 固定保存结果：不携带动态 ``is_current``，重复提交保持原值 */
export interface PlanSaveResultWire {
  proposal_id: string;
  id: string;
  content: PlanContentWire;
  created_at: number;
  saved_at: number;
}

/** `get_plan_save_status` 输入 */
export interface PlanStatusArgumentsWire {
  proposal_id: string;
}

/** `get_plan_save_status` 输出：``proposal_id`` 与查询参数一致，saved 时结果的快照标识也一致；
 *  保存状态沿用画像与训练记录的 `pending / processing / saved / invalidated / conflicted` */
export type PlanStatusResultWire =
  | {
      proposal_id: string;
      status: "saved";
      result: PlanSaveResultWire;
    }
  | {
      proposal_id: string;
      status: "pending" | "processing" | "invalidated" | "conflicted";
      result: null;
    };

/** 内容校验错误的字段定位：点号与数组索引形式，如 ``payload.days.0.exercises.0.sets`` */
export interface PlanFieldErrorWire {
  path: string;
  message: string;
}

/** 计划业务错误码（契约 §8） */
export type PlanBusinessErrorCode =
  | "plan_not_found"
  | "plan_proposal_not_found"
  | "plan_proposal_invalidated"
  | "plan_save_processing"
  | "plan_version_conflict"
  | "profile_required"
  | "profile_version_conflict"
  | "plan_confirmation_invalid"
  | "plan_access_denied"
  | "proposal_already_saved"
  | "session_not_found";

/** 工具失败结果内容：仅 invalid_business_payload 携带 ``errors`` */
export type PlanBusinessErrorWire =
  | {
      code: "invalid_business_payload";
      message: string;
      errors: PlanFieldErrorWire[];
    }
  | {
      code: PlanBusinessErrorCode;
      message: string;
    };

/** GET /api/sessions/{session_id}/history：会话头 ＋ 当前分支节点 ＋ 关联运行与输入（§3） */
export interface SessionHistoryWire {
  session: SessionWire;
  entries: HistoryEntryWire[];
  runs: HistoryRunWire[];
  steering: HistorySteeringWire[];
}

/* ===== 会话运行及 Steering 独立列表（session-list-contract §2、§3）===== */

/** GET /api/sessions/{session_id}/runs：该会话仍保存的全部运行，按 started_at、run_id 升序（§2） */
export interface SessionRunListWire {
  session_id: string;
  runs: SessionRunWire[];
}

/** 指定运行的 Steering 列表项（session-list-contract §3）：十个必填字段，不含附件投影 */
export type RunSteeringItemWire = Omit<HistorySteeringWire, "attachments">;

/** GET /api/sessions/{session_id}/runs/{run_id}/steering：指定运行的全部保留输入，按 created_at、steering_id 升序（§3） */
export interface RunSteeringListWire {
  session_id: string;
  run_id: string;
  steering: RunSteeringItemWire[];
}

/* ===== Agent SSE 事件（backend-http-sse-contract §8、§11.6）===== */

export type ReActStopReason =
  "stop" | "toolUse" | "length" | "error" | "aborted";

export type ReActContent = { content_index: number } & (
  | { type: "text"; text: string }
  | { type: "thinking"; thinking: string }
  | {
      type: "tool_call";
      tool_call_id: string;
      name: string;
      arguments: Record<string, unknown>;
    }
);

export type ReActUpdateType =
  | "text_start"
  | "text_delta"
  | "text_end"
  | "thinking_start"
  | "thinking_delta"
  | "thinking_end"
  | "toolcall_start"
  | "toolcall_delta"
  | "toolcall_end";

/** 一条 Steering 状态通知（§11.6）：三种状态的字段组合固定 */
export type SteeringStatus =
  | { status: "consumed"; entry_id: string; reason: null }
  | { status: "withdrawn"; entry_id: null; reason: null }
  | { status: "discarded"; entry_id: null; reason: DiscardReasonWire };

type ReActMessageData = { message_id: string; content: ReActContent[] };

/** 已提交节点身份：首节点的 ``parent_id`` 为 null（§8） */
interface ReActCommittedEntry {
  entry_id: string;
  parent_id: string | null;
}

/** 单工具进度／完成事件共用的公开快照（§8） */
interface ReActToolSnapshot {
  tool_call_id: string;
  tool_name: string;
  content: string;
  is_error: boolean;
}

export type ReActEvent = { data: { run_id: string } } & (
  | { event: "message_start"; data: ReActMessageData }
  | {
      event: "message_update";
      data: ReActMessageData & {
        content_index: number;
        update_type: ReActUpdateType;
      };
    }
  | {
      event: "message_end";
      data: ReActMessageData &
        ReActCommittedEntry & { stop_reason: ReActStopReason };
    }
  | {
      event: "tool_start";
      data: {
        tool_call_id: string;
        name: string;
        arguments: Record<string, unknown>;
      };
    }
  | { event: "tool_execution_update"; data: ReActToolSnapshot }
  | { event: "tool_execution_end"; data: ReActToolSnapshot }
  | {
      event: "tool_result";
      data: {
        tool_call_id: string;
        content: string;
        is_error: boolean;
      } & ReActCommittedEntry;
    }
  /** 压缩开始（compaction-contract-decisions §B）：``compaction_id`` 为该次压缩提交节点的身份，
   *  ``reason`` 为触发原因；压缩期间运行保持 running，前端展示“压缩上下文”阶段 */
  | {
      event: "compaction_start";
      data: { compaction_id: string; reason: "threshold" | "overflow" };
    }
  /** 压缩检查点提交成功后发送（§B）：``compaction_id`` 与提交节点 ``entry_id`` 一致，``parent_id`` 为其父节点 */
  | {
      event: "compaction_end";
      data: ReActCommittedEntry & { compaction_id: string };
    }
  | { event: "steering_status"; data: { steering_id: string } & SteeringStatus }
  | {
      event: "done";
      data: { status: "completed"; stop_reason: "stop" | "length" };
    }
  | {
      event: "error";
      data: {
        status: "failed" | "cancelled";
        code:
          | "execution_failed"
          | "cancelled"
          | "credential_detected"
          | "compaction_failed"
          | "context_budget_exceeded"
          | "context_overflow";
        message: string;
        tool_call_id: string | null;
      };
    }
);

/** 已注册业务接口错误码（§11.5，编辑与重新生成新增 ``entry_not_found`` / ``invalid_target_entry``，
 *  画像自然语言确认新增 §11.9 的八项业务码，训练记录新增 workout-http-sse-contract §7 的 ``workout_not_found``，
 *  训练计划新增 plan-generation-contract §8 的十项业务码，
 *  附件新增 plan-import-adjustment-contract §9 的五项附件码，
 *  模型配置新增 PRODUCT.md §3.4 的 ``model_not_configured``） */
export type ErrorCode =
  | "host_forbidden"
  | "origin_forbidden"
  | "session_not_found"
  | "run_not_found"
  | "steering_not_found"
  | "entry_not_found"
  | "invalid_target_entry"
  | "session_conflict"
  | "session_mismatch"
  | "run_busy"
  | "run_closed"
  | "operation_conflict"
  | "steering_consumption_conflict"
  | "incomplete_tool_chain"
  | "workout_not_found"
  | "profile_proposal_not_found"
  | "profile_proposal_invalidated"
  | "profile_update_processing"
  | "profile_version_conflict"
  | "profile_confirmation_invalid"
  | "profile_access_denied"
  | "plan_not_found"
  | "plan_proposal_not_found"
  | "plan_proposal_invalidated"
  | "plan_save_processing"
  | "plan_version_conflict"
  | "profile_required"
  | "plan_confirmation_invalid"
  | "plan_access_denied"
  | "proposal_already_saved"
  | "attachment_format_invalid"
  | "attachment_size_exceeded"
  | "attachment_not_found"
  | "attachment_access_denied"
  | "attachment_conflict"
  | "invalid_request"
  | "invalid_business_payload"
  | "credential_detected"
  | "model_not_configured"
  | "internal_error";

/* ===== 模型配置（PRODUCT.md §3.4）===== */

export type ModelApi = "openai-completions" | "anthropic-messages";

/** provider 留空时使用协议值；保存与诊断均要求显式提交 Key。 */
export interface ProviderWriteBody {
  api: ModelApi;
  base_url: string;
  model: string;
  api_key: string;
  provider?: string;
}

/** api_key 为明文凭据，仅保存在内存；provider 为生效值，清除后全部字段为 null。 */
export interface ProviderStatusWire {
  api: ModelApi | null;
  base_url: string | null;
  model: string | null;
  api_key: string | null;
  provider: string | null;
}

/** 诊断通过和失败均返回非负整数毫秒耗时。 */
export interface ProviderTestWire {
  ok: boolean;
  latency_ms: number;
  message: string;
}
