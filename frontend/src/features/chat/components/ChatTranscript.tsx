import { Fragment, useRef, useState } from "react";
import { ChevronDown, Loader2, Pencil, RefreshCw, Undo2 } from "lucide-react";
import { Bubble, BubbleContent } from "@/components/ui/bubble";
import { Button } from "@/components/ui/button";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Message, MessageContent } from "@/components/ui/message";
import {
  MessageScroller,
  MessageScrollerButton,
  MessageScrollerContent,
  MessageScrollerItem,
  MessageScrollerProvider,
  MessageScrollerViewport,
} from "@/components/ui/message-scroller";
import { Textarea } from "@/components/ui/textarea";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import ToolCallCard from "./ToolCallCard";
import MarkdownContent from "./MarkdownContent";
import UserMessageBubble from "./UserMessageBubble";
import {
  AttachmentChips,
  AttachmentPicker,
  useAttachmentDrafts,
} from "./Attachments";
import ProfileFields from "@/features/profile/ProfileFields";
import PlanFields from "@/features/plans/PlanFields";
import { WorkoutProposalFields } from "@/features/workout/WorkoutFields";
import {
  canSubmitChatInput,
  type ConfirmationDisplay,
  type ReActEntry,
  type ReActRound,
} from "../utils/reactAgent";
import {
  CONFIRMATION_LABELS,
  confirmationAttemptDisplay,
  confirmationButtonState,
  type ConfirmationAttempt,
} from "../utils/confirmations";
import type { AttachmentDraft } from "../utils/attachments";
import type {
  ConfirmationBusinessKindWire,
  PendingProposalReferenceWire,
} from "@/lib/contract";

/** 就地编辑状态（用户约定）：正在编辑的消息节点、文本；附件草稿由本页统一的草稿状态持有 */
type EditState = { entryId: string; text: string } | null;

type DraftState = ReturnType<typeof useAttachmentDrafts>;

/** 待确认快照的确认入口（确认契约 §2、§7.6）：按钮位于该份完整内容外部，与快照标识和展示节点绑定 */
function ConfirmButton({
  kind,
  proposalId,
  displayEntryId,
  proposals,
  attempts,
  open,
  onConfirm,
}: {
  kind: ConfirmationBusinessKindWire;
  proposalId: string;
  displayEntryId?: string;
  proposals: PendingProposalReferenceWire[];
  attempts: ConfirmationAttempt[];
  open: boolean;
  onConfirm: (
    kind: ConfirmationBusinessKindWire,
    proposalId: string,
    displayEntryId: string,
  ) => void;
}) {
  if (displayEntryId === undefined) return null;
  const state = confirmationButtonState(
    kind,
    proposalId,
    displayEntryId,
    proposals,
    attempts,
    open,
  );
  if (state === "unavailable") return null;
  return (
    <Button
      type="button"
      size="sm"
      variant="default"
      className="self-end"
      disabled={state !== "available"}
      onClick={() => onConfirm(kind, proposalId, displayEntryId)}
    >
      {CONFIRMATION_LABELS[kind]}
    </Button>
  );
}

/** 确认结果行（§7.6）：与 Agent round 独立，只陈述真实操作状态与实际失败原因 */
function ConfirmationResult({ display }: { display: ConfirmationDisplay }) {
  return (
    <p
      role={display.status === "failed" ? "alert" : "status"}
      className={
        display.status === "failed"
          ? "text-sm text-destructive"
          : "text-sm text-muted-foreground"
      }
    >
      {`${CONFIRMATION_LABELS[display.business_kind]} · ${display.text}`}
      {display.detail === undefined ? "" : `：${display.detail}`}
    </p>
  );
}

/** 运行用时（Trace 采集方案 §5）：起止时间属于同一次真实运行，取业务记录的 UTC 毫秒差按既有约定转秒；
 *  运行中、起点未知或结束时间为 null/缺省时不渲染，保持原有界面 */
function RunDuration({ round }: { round: ReActRound }) {
  if (round.status === "running") return null;
  if (round.started_at == null || round.finished_at == null) return null;
  return (
    <MessageScrollerItem messageId={`${round.id}:duration`}>
      <Message align="start">
        <MessageContent>
          <p className="text-sm text-muted-foreground">
            {`用时 ${((round.finished_at - round.started_at) / 1000).toFixed(1)} 秒`}
          </p>
        </MessageContent>
      </Message>
    </MessageScrollerItem>
  );
}

function Entry({
  entry,
  roundId,
  sessionId,
  onWithdraw,
  canEdit,
  canRegenerate,
  editing,
  editSubmitting,
  drafts,
  proposals,
  attempts,
  confirmOpen,
  onConfirm,
  onStartEdit,
  onEditTextChange,
  onCloseEdit,
  onEdit,
  onRegenerate,
}: {
  entry: ReActEntry;
  roundId: string;
  sessionId: string;
  onWithdraw: (roundId: string, steeringId: string) => void;
  canEdit: boolean;
  /** 确认节点不提供模型重新生成入口（§7.6）：结果只通过专用状态查询恢复 */
  canRegenerate: boolean;
  editing: EditState;
  editSubmitting: boolean;
  drafts: DraftState;
  proposals: PendingProposalReferenceWire[];
  attempts: ConfirmationAttempt[];
  confirmOpen: boolean;
  onConfirm: (
    kind: ConfirmationBusinessKindWire,
    proposalId: string,
    displayEntryId: string,
  ) => void;
  onStartEdit: (entry: ReActEntry) => void;
  onEditTextChange: (text: string) => void;
  onCloseEdit: () => void;
  onEdit: (entryId: string, text: string) => void;
  onRegenerate: (entryId: string) => void;
}) {
  if (entry.kind === "tool") {
    /* 业务待确认内容的完整展示（profile-plan §3.2、workout-http-sse-contract §5、plan-generation-contract §6）：
     * 准备工具的结果节点以 AI 一侧普通消息样式呈现全部字段，确认入口位于该份完整内容外部并与
     * 快照标识、展示节点绑定（确认契约 §2、页面布局约定）；其余工具沿用通用工具展示。 */
    const prepared =
      entry.profile !== undefined
        ? {
            kind: "profile" as const,
            proposalId: entry.profile.proposal_id,
            body: <ProfileFields content={entry.profile.payload} />,
          }
        : entry.workout !== undefined
          ? {
              kind: "workout" as const,
              proposalId: entry.workout.proposal_id,
              body: <WorkoutProposalFields proposal={entry.workout} />,
            }
          : entry.plan !== undefined
            ? {
                kind: "plan" as const,
                proposalId: entry.plan.proposal_id,
                body: <PlanFields content={entry.plan.payload} />,
              }
            : null;
    if (prepared === null) return <ToolCallCard {...entry} />;
    return (
      <Message align="start">
        <MessageContent>
          <Bubble
            variant="secondary"
            align="start"
            className="w-full max-w-full"
          >
            <BubbleContent className="w-full">{prepared.body}</BubbleContent>
          </Bubble>
          <ConfirmButton
            kind={prepared.kind}
            proposalId={prepared.proposalId}
            displayEntryId={entry.entry_id}
            proposals={proposals}
            attempts={attempts}
            open={confirmOpen}
            onConfirm={onConfirm}
          />
        </MessageContent>
      </Message>
    );
  }
  if (entry.kind === "user") {
    if (entry.request === undefined) return null;
    const request = entry.request;
    const attachments = entry.attachments ?? [];
    const nodeId = entry.entry_id;
    const activeEdit =
      editing !== null && nodeId !== undefined && editing.entryId === nodeId
        ? editing
        : null;
    return (
      <Message align="end">
        <MessageContent>
          {activeEdit !== null ? (
            <div className="flex w-full flex-col gap-2">
              <Textarea
                value={activeEdit.text}
                onChange={(event) => onEditTextChange(event.target.value)}
                disabled={editSubmitting}
                aria-label="编辑消息"
                rows={2}
                className="min-h-0 resize-none"
              />
              <AttachmentChips
                drafts={drafts.drafts}
                error={drafts.error}
                onRemove={drafts.remove}
                disabled={editSubmitting}
              />
              <div className="flex items-center justify-between gap-2">
                <AttachmentPicker
                  onPick={drafts.pick}
                  disabled={editSubmitting}
                />
                <div className="flex justify-end gap-2">
                  <Button
                    type="button"
                    variant="secondary"
                    size="sm"
                    onClick={onCloseEdit}
                  >
                    取消
                  </Button>
                  <Button
                    type="button"
                    size="sm"
                    disabled={
                      !canEdit ||
                      editSubmitting ||
                      drafts.pending ||
                      !canSubmitChatInput(activeEdit.text, drafts.drafts, [])
                    }
                    onClick={() => onEdit(activeEdit.entryId, activeEdit.text)}
                  >
                    提交编辑
                  </Button>
                </div>
              </div>
            </div>
          ) : (
            <UserMessageBubble
              sessionId={sessionId}
              request={request}
              attachments={attachments}
              actions={
                canEdit && nodeId !== undefined ? (
                  <div className="absolute right-0 top-full z-10 hidden gap-1 pt-1 group-hover/bubble:flex group-focus-within/bubble:flex">
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon"
                          className="size-7 text-muted-foreground hover:text-foreground"
                          aria-label="编辑消息"
                          onClick={() => onStartEdit(entry)}
                        >
                          <Pencil />
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent>编辑</TooltipContent>
                    </Tooltip>
                    {canRegenerate && (
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <Button
                            type="button"
                            variant="ghost"
                            size="icon"
                            className="size-7 text-muted-foreground hover:text-foreground"
                            aria-label="重新生成"
                            onClick={() => onRegenerate(nodeId)}
                          >
                            <RefreshCw />
                          </Button>
                        </TooltipTrigger>
                        <TooltipContent>重新生成</TooltipContent>
                      </Tooltip>
                    )}
                  </div>
                ) : undefined
              }
            />
          )}
          {entry.steering?.status === "pending" && (
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  className="self-end size-7 text-muted-foreground hover:text-foreground"
                  aria-label="撤回"
                  onClick={() => onWithdraw(roundId, entry.id)}
                >
                  <Undo2 />
                </Button>
              </TooltipTrigger>
              <TooltipContent>撤回</TooltipContent>
            </Tooltip>
          )}
          {entry.steering?.status === "discarded" && (
            <p role="status" className="self-end text-sm text-muted-foreground">
              未被本次执行消费
            </p>
          )}
        </MessageContent>
      </Message>
    );
  }
  return (
    <Message align="start">
      <MessageContent>
        {entry.content.map((block) => {
          if (block.type === "text")
            return block.text ? (
              <Bubble
                key={block.content_index}
                variant="secondary"
                align="start"
              >
                <BubbleContent>
                  <MarkdownContent
                    text={block.text}
                    streaming={!entry.stop_reason}
                  />
                </BubbleContent>
              </Bubble>
            ) : null;
          if (block.type === "thinking")
            return block.thinking ? (
              <Collapsible
                key={block.content_index}
                defaultOpen={false}
                className="order-first text-sm"
              >
                <CollapsibleTrigger asChild>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    aria-label="思考内容"
                    className="group"
                  >
                    思考
                    <ChevronDown
                      aria-hidden
                      className="size-4 transition-transform group-data-[state=open]:rotate-180"
                    />
                  </Button>
                </CollapsibleTrigger>
                <CollapsibleContent className="whitespace-pre-wrap break-words px-3 py-2 text-muted-foreground">
                  {block.thinking}
                </CollapsibleContent>
              </Collapsible>
            ) : null;
          const truncated = entry.stop_reason === "length";
          const state = truncated ? "未执行" : "生成中";
          return !entry.stop_reason || truncated ? (
            <Collapsible
              key={block.content_index}
              defaultOpen={false}
              className="text-sm"
            >
              <CollapsibleTrigger asChild>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  aria-label={`${block.name} ${state}的调用参数`}
                >
                  {`${block.name} · ${state}`}
                  <ChevronDown aria-hidden className="size-4" />
                </Button>
              </CollapsibleTrigger>
              <CollapsibleContent>
                <pre className="whitespace-pre-wrap break-all">
                  {JSON.stringify(block.arguments, null, 2)}
                </pre>
              </CollapsibleContent>
            </Collapsible>
          ) : null;
        })}
        {entry.stop_reason === "length" && (
          <p role="status" className="text-sm text-muted-foreground">
            回答达到输出上限，内容未完成
          </p>
        )}
        {entry.stop_reason === "aborted" && (
          <p role="status" className="text-sm text-muted-foreground">
            已取消
          </p>
        )}
      </MessageContent>
    </Message>
  );
}

export default function ChatTranscript({
  sessionId,
  rounds,
  onRetry,
  onWithdraw,
  retrying,
  canEdit,
  canConfirm,
  proposals,
  confirmations,
  onConfirm,
  onRetryConfirmation,
  onEdit,
  onRegenerate,
}: {
  sessionId: string;
  rounds: ReActRound[];
  onRetry: (roundId: string, operationId: string) => void;
  onWithdraw: (roundId: string, steeringId: string) => void;
  retrying?: string;
  canEdit: boolean;
  /** 确认入口可用性（确认契约 §7.1、§7.6）：Agent 执行期间与其它未决确认处理期间均不开放 */
  canConfirm: boolean;
  proposals: PendingProposalReferenceWire[];
  confirmations: ConfirmationAttempt[];
  onConfirm: (
    kind: ConfirmationBusinessKindWire,
    proposalId: string,
    displayEntryId: string,
  ) => void;
  onRetryConfirmation: (operationId: string) => void;
  /** 编辑提交完整附件集合：保留项为引用，新增或替换项为上传，全部移除为空集合 */
  onEdit: (
    entryId: string,
    text: string,
    attachments: AttachmentDraft[],
  ) => Promise<boolean>;
  onRegenerate: (entryId: string) => void;
}) {
  const [editing, setEditing] = useState<EditState>(null);
  const [editSubmitting, setEditSubmitting] = useState(false);
  const drafts = useAttachmentDrafts();
  /** 编辑实例令牌：提交、取消与切换对象各自推进，迟到的受理结果仅在仍是同一实例时生效 */
  const editInstanceRef = useRef(0);
  const startEdit = (entry: ReActEntry) => {
    if (entry.kind !== "user" || entry.entry_id === undefined) return;
    editInstanceRef.current += 1;
    setEditSubmitting(false);
    setEditing({
      entryId: entry.entry_id,
      text: entry.request ?? "",
    });
    // 编辑界面默认载入并保留原附件（附件契约 §3.2）；切换编辑对象使旧读取任务失效
    drafts.reset(entry.attachments ?? []);
  };
  const closeEdit = () => {
    editInstanceRef.current += 1;
    setEditSubmitting(false);
    setEditing(null);
    drafts.clear();
  };
  const submitEdit = (entryId: string, text: string) => {
    const instance = editInstanceRef.current;
    setEditSubmitting(true);
    void onEdit(entryId, text, drafts.drafts).then((accepted) => {
      // 受理迟到时仅在同一编辑实例仍成立时关闭，取消或重新打开后的结果不回滚新草稿与输入
      if (editInstanceRef.current !== instance) return;
      setEditSubmitting(false);
      if (accepted) closeEdit();
    });
  };
  return (
    <MessageScrollerProvider
      autoScroll
      defaultScrollPosition="end"
      scrollPreviousItemPeek={64}
    >
      <MessageScroller className="min-h-0 flex-1">
        <MessageScrollerViewport className="pt-10 pb-10">
          <MessageScrollerContent
            aria-busy={rounds.some((round) => round.status === "running")}
            className="mx-auto w-full max-w-4xl gap-10 px-6"
          >
            {rounds.map((round) => (
              <Fragment key={round.id}>
                {round.entries
                  .filter(
                    (entry) =>
                      entry.kind === "tool" ||
                      (entry.kind === "user"
                        ? !!entry.request ||
                          (entry.attachments?.length ?? 0) > 0
                        : entry.content.some((block) =>
                            block.type === "text"
                              ? !!block.text
                              : block.type === "thinking"
                                ? !!block.thinking
                                : !entry.stop_reason,
                          ) ||
                          entry.stop_reason === "length" ||
                          entry.stop_reason === "aborted"),
                  )
                  .map((entry) => (
                    <MessageScrollerItem key={entry.id} messageId={entry.id}>
                      <Entry
                        entry={entry}
                        roundId={round.id}
                        sessionId={sessionId}
                        onWithdraw={onWithdraw}
                        canEdit={canEdit}
                        canRegenerate={round.confirmation === undefined}
                        editing={editing}
                        editSubmitting={editSubmitting}
                        drafts={drafts}
                        proposals={proposals}
                        attempts={confirmations}
                        confirmOpen={canConfirm}
                        onConfirm={onConfirm}
                        onStartEdit={startEdit}
                        onEditTextChange={(text) =>
                          setEditing((current) =>
                            current === null ? null : { ...current, text },
                          )
                        }
                        onCloseEdit={closeEdit}
                        onEdit={submitEdit}
                        onRegenerate={onRegenerate}
                      />
                    </MessageScrollerItem>
                  ))}
                <RunDuration round={round} />
                {round.confirmation !== undefined && (
                  <MessageScrollerItem messageId={`${round.id}:confirmation`}>
                    <ConfirmationResult display={round.confirmation} />
                  </MessageScrollerItem>
                )}
                {round.pending?.map((operation) => (
                  <MessageScrollerItem
                    key={`pending:${operation.operation_id}`}
                    messageId={`${round.id}:pending:${operation.operation_id}`}
                  >
                    <div className="flex items-center gap-2">
                      <p
                        role="status"
                        className="text-sm text-muted-foreground"
                      >
                        提交结果未知
                      </p>
                      <Button
                        type="button"
                        variant="secondary"
                        size="sm"
                        disabled={retrying === operation.operation_id}
                        onClick={() =>
                          onRetry(round.id, operation.operation_id)
                        }
                      >
                        重试
                      </Button>
                    </div>
                  </MessageScrollerItem>
                ))}
                {(round.status !== "completed" || round.error) &&
                  !(round.status === "failed" &&
                    round.error === "回答达到输出上限，内容未完成" &&
                    round.entries.some(
                      (entry) =>
                        entry.kind === "assistant" &&
                        entry.stop_reason === "length",
                    )) && (
                    <MessageScrollerItem messageId={`${round.id}:status`}>
                      {round.status === "running" && (
                        <div className="flex items-center gap-2">
                          <Loader2
                            role="status"
                            aria-label="执行中"
                            className="size-4 animate-spin text-muted-foreground"
                          />
                          {/* 压缩阶段（compaction-contract-decisions §B）：仅由当前连接的 compaction_start 得知 */}
                          {round.compaction_id !== undefined && (
                            <p
                              role="status"
                              className="text-sm text-muted-foreground"
                            >
                              压缩上下文
                            </p>
                          )}
                        </div>
                      )}
                      {round.status === "cancelled" && (
                        <p
                          role="status"
                          className="text-sm text-muted-foreground"
                        >
                          已取消
                        </p>
                      )}
                      {round.status === "interrupted" && (
                        <p
                          role="status"
                          className="text-sm text-muted-foreground"
                        >
                          已中断
                        </p>
                      )}
                      {round.status === "failed" && !round.error && (
                        <p
                          role="status"
                          className="text-sm text-muted-foreground"
                        >
                          本次运行失败
                        </p>
                      )}
                      {round.error && round.status !== "interrupted" && (
                        <p role="alert" className="text-sm text-destructive">
                          {round.error}
                        </p>
                      )}
                    </MessageScrollerItem>
                  )}
              </Fragment>
            ))}
            {/* 提交在途与结果未知的确认（确认契约 §7.5）：历史覆盖确认后由确认节点轮次展示真实结果 */}
            {confirmations.map((attempt) => (
              <MessageScrollerItem
                key={`confirm:${attempt.request.operation_id}`}
                messageId={`confirm:${attempt.request.operation_id}`}
              >
                <div className="flex items-center gap-2">
                  <ConfirmationResult
                    display={confirmationAttemptDisplay(attempt)}
                  />
                  {attempt.operation === null && (
                    <Button
                      type="button"
                      variant="secondary"
                      size="sm"
                      onClick={() =>
                        onRetryConfirmation(attempt.request.operation_id)
                      }
                    >
                      重试
                    </Button>
                  )}
                </div>
              </MessageScrollerItem>
            ))}
          </MessageScrollerContent>
        </MessageScrollerViewport>
        <MessageScrollerButton />
      </MessageScroller>
    </MessageScrollerProvider>
  );
}
