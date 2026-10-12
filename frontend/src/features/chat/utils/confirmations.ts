/* ===== 结构化保存确认账本与状态（structured-save-confirmation §7.2–§7.6）===== */

import type {
  ConfirmationBusinessKindWire,
  PendingProposalReferenceWire,
  SaveConfirmationOperationWire,
  SaveConfirmationRequestWire,
} from "@/lib/contract";
import {
  exactKeys,
  isObject,
  parseSaveConfirmationRequest,
} from "@/lib/business";
import { SESSION_DELETED_EVENT, type ConfirmationDisplay } from "./reactAgent";

/** 按会话归属的未决确认账本（§7.5、session-history-contract §5.1） */
const CONFIRMATION_LEDGER_KEY = "fit-agent:save-confirmations";

/** 确认账本条目：原会话、操作 ID 与完整四字段请求，同一次提交与显式重试逐字沿用（§7.2、§7.5） */
export interface ConfirmationLedger {
  session_id: string;
  request: SaveConfirmationRequestWire;
}

/** 提交后的阶段：pending 为提交在途或结果未知，processing 为后端已登记且在执行，
 *  saved 与 failed 为已核实的真实终态（§7.3、§7.5） */
type ConfirmationPhase = "pending" | "processing" | "saved" | "failed";

/** 页面展示用的确认尝试：账本条目加最近一次真实操作对象与未知原因 */
export interface ConfirmationAttempt extends ConfirmationLedger {
  phase: ConfirmationPhase;
  operation: SaveConfirmationOperationWire | null;
  notice?: string;
}

/** 按钮可用性（§7.6）：仅 pending 且展示绑定匹配的快照开放提交 */
type ConfirmationButtonState = "available" | "busy" | "unavailable";

/** 快照外的确认入口文字，与后端生成的确认节点正文同口径（§7.6） */
export const CONFIRMATION_LABELS: Record<ConfirmationBusinessKindWire, string> =
  {
    profile: "确认保存画像",
    plan: "确认保存计划",
    workout: "确认保存训练记录",
  };

function parseLedger(value: unknown): ConfirmationLedger {
  if (!isObject(value)) throw new Error("确认账本条目无效。");
  exactKeys(value, ["session_id", "request"], "确认账本条目");
  if (typeof value.session_id !== "string")
    throw new Error("确认账本会话身份无效。");
  return { session_id: value.session_id, request: parseSaveConfirmationRequest(value.request) };
}

function readLedgers(): Record<string, ConfirmationLedger[]> {
  if (typeof localStorage === "undefined") return {};
  const raw = localStorage.getItem(CONFIRMATION_LEDGER_KEY);
  if (raw === null) return {};
  const value: unknown = JSON.parse(raw);
  if (!isObject(value)) throw new Error("确认账本无效。");
  const ledgers: Record<string, ConfirmationLedger[]> = {};
  for (const [sessionId, list] of Object.entries(value)) {
    if (!Array.isArray(list)) throw new Error("会话确认账本无效。");
    ledgers[sessionId] = list.map(parseLedger);
  }
  return ledgers;
}

function writeLedgers(ledgers: Record<string, ConfirmationLedger[]>): void {
  if (typeof localStorage === "undefined") return;
  localStorage.setItem(CONFIRMATION_LEDGER_KEY, JSON.stringify(ledgers));
}

/** 读取指定会话的未决确认账本（§7.5）：跨刷新、页面切换与重启后按原操作恢复 */
export function readConfirmationLedger(
  sessionId: string,
): ConfirmationLedger[] {
  return readLedgers()[sessionId] ?? [];
}

/** 变更指定会话的确认账本并落盘（§7.5）：其它会话账本与当前选择保持原值 */
export function updateConfirmationLedger(
  sessionId: string,
  change: (list: ConfirmationLedger[]) => ConfirmationLedger[],
): ConfirmationLedger[] {
  const ledgers = readLedgers();
  const next = change(ledgers[sessionId] ?? []);
  if (next.length === 0) delete ledgers[sessionId];
  else ledgers[sessionId] = next;
  writeLedgers(ledgers);
  return next;
}

/** 会话删除后的账本清理（session-delete-contract §5）：未完成确认随身份移出 */
export function forgetConfirmationLedger(sessionId: string): void {
  const ledgers = readLedgers();
  if (!(sessionId in ledgers)) return;
  delete ledgers[sessionId];
  writeLedgers(ledgers);
}

/** 新建一次确认提交（§7.2）：客户端生成幂等操作 ID，其余业务事实由后端从已绑定快照取得 */
export function newConfirmationRequest(
  kind: ConfirmationBusinessKindWire,
  proposalId: string,
  displayEntryId: string,
): SaveConfirmationRequestWire {
  return {
    operation_id: crypto.randomUUID(),
    business_kind: kind,
    proposal_id: proposalId,
    display_entry_id: displayEntryId,
  };
}

/** 账本条目 → 展示尝试（§5.2-2）：恢复时先按结果未知处理，由专用 GET 核实 */
export function attemptFromLedger(
  ledger: ConfirmationLedger,
): ConfirmationAttempt {
  return {
    ...ledger,
    phase: "pending",
    operation: null,
    notice: "确认结果未知，正在核实原操作。",
  };
}

/** 占用判定（§7.1）：未决与处理中的确认阻止新的发送、编辑、重新生成及其它确认操作 */
export function confirmationOccupying(list: ConfirmationAttempt[]): boolean {
  return list.some(
    (item) => item.phase === "pending" || item.phase === "processing",
  );
}

/** 同一操作的阶段更新：重复登记保持原操作身份，不换 ID（§7.5） */
export function upsertConfirmationAttempt(
  list: ConfirmationAttempt[],
  attempt: ConfirmationAttempt,
): ConfirmationAttempt[] {
  const operationId = attempt.request.operation_id;
  const index = list.findIndex(
    (item) => item.request.operation_id === operationId,
  );
  if (index === -1) return [...list, attempt];
  return list.map((item, at) => (at === index ? attempt : item));
}

/** 操作对象落地（§7.3）：状态与结果／错误的配对由解析层保证，此处只映射展示阶段 */
export function applyConfirmationOperation(
  list: ConfirmationAttempt[],
  operation: SaveConfirmationOperationWire,
): ConfirmationAttempt[] {
  return list.map((item) =>
    item.request.operation_id === operation.operation_id
      ? { ...item, phase: operation.status, operation, notice: undefined }
      : item,
  );
}

/** 真实终态由历史覆盖后移出展示与账本（§7.6） */
export function removeConfirmationAttempt(
  list: ConfirmationAttempt[],
  operationId: string,
): ConfirmationAttempt[] {
  return list.filter((item) => item.request.operation_id !== operationId);
}

/** 快照的按钮状态（§7.1、§7.6）：引用为 pending 时开放；本快照已有未决确认，
 *  或其它确认正在处理中时全部快照一并保持占用；
 *  失效与已保存快照不再出现在待确认引用中，因此不提供新的提交操作 */
export function confirmationButtonState(
  kind: ConfirmationBusinessKindWire,
  proposalId: string,
  displayEntryId: string,
  proposals: PendingProposalReferenceWire[],
  attempts: ConfirmationAttempt[],
  open: boolean,
): ConfirmationButtonState {
  const reference = proposals.find(
    (item) =>
      item.business_kind === kind &&
      item.proposal_id === proposalId &&
      item.display_entry_id === displayEntryId,
  );
  if (reference === undefined || reference.status !== "pending")
    return "unavailable";
  const same = attempts.some(
    (item) =>
      item.request.business_kind === kind &&
      item.request.proposal_id === proposalId,
  );
  if (same || confirmationOccupying(attempts) || !open) return "busy";
  return "available";
}

/** 真实操作对象的展示投影（§7.6）：仅陈述状态与实际失败原因，不含模型生成内容 */
export function confirmationOperationDisplay(
  operation: SaveConfirmationOperationWire,
): ConfirmationDisplay {
  const base = {
    business_kind: operation.business_kind,
    proposal_id: operation.proposal_id,
    status: operation.status,
  };
  if (operation.status === "saved") return { ...base, text: "已保存" };
  if (operation.status === "processing") return { ...base, text: "确认处理中" };
  const error = operation.error;
  if (error === null) throw new Error("失败确认操作缺少错误对象。");
  return {
    ...base,
    text: "保存失败",
    detail:
      error.errors === undefined || error.errors.length === 0
        ? error.message
        : `${error.message}：${error.errors
            .map((item) => item.message)
            .join("；")}`,
  };
}

/** 提交尝试的展示投影：未核实到操作对象时保持结果未知（§7.5） */
export function confirmationAttemptDisplay(
  attempt: ConfirmationAttempt,
): ConfirmationDisplay {
  if (attempt.operation !== null)
    return confirmationOperationDisplay(attempt.operation);
  return {
    business_kind: attempt.request.business_kind,
    proposal_id: attempt.request.proposal_id,
    status: "processing",
    text: "确认结果未知",
    detail: attempt.notice,
  };
}

if (typeof window !== "undefined") {
  window.addEventListener(SESSION_DELETED_EVENT, (event) =>
    forgetConfirmationLedger((event as CustomEvent<string>).detail),
  );
}
