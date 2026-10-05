import { Fragment, useState, type Dispatch, type SetStateAction } from "react";
import { ChevronDown, Loader2 } from "lucide-react";
import ReactMarkdown from "react-markdown";
import { Bubble, BubbleContent } from "@/components/ui/bubble";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Message, MessageContent } from "@/components/ui/message";
import { MessageScroller, MessageScrollerButton, MessageScrollerContent, MessageScrollerItem, MessageScrollerProvider, MessageScrollerViewport } from "@/components/ui/message-scroller";
import { Textarea } from "@/components/ui/textarea";
import ToolCallCard from "./ToolCallCard";
import type { ReActEntry, ReActRound } from "../utils/reactAgent";

/** 就地编辑状态（用户约定）：正在编辑的消息节点及其文本 */
type EditState = { entryId: string; text: string } | null;

function Entry({ entry, roundId, onWithdraw, canEdit, editing, setEditing, onEdit, onRegenerate }: {
  entry: ReActEntry;
  roundId: string;
  onWithdraw: (roundId: string, steeringId: string) => void;
  canEdit: boolean;
  editing: EditState;
  setEditing: Dispatch<SetStateAction<EditState>>;
  onEdit: (entryId: string, text: string) => Promise<boolean>;
  onRegenerate: (entryId: string) => void;
}) {
  if (entry.kind === "tool") return <ToolCallCard {...entry} />;
  if (entry.kind === "user") {
    if (entry.request === undefined) return null;
    const request = entry.request;
    const nodeId = entry.entry_id;
    const activeEdit = editing !== null && nodeId !== undefined && editing.entryId === nodeId ? editing : null;
    return (
      <Message align="end"><MessageContent>
        {activeEdit !== null ? (
          <div className="flex w-full flex-col gap-2">
            <Textarea value={activeEdit.text} onChange={(event) => setEditing({ entryId: activeEdit.entryId, text: event.target.value })} aria-label="编辑消息" rows={2} className="min-h-0 resize-none" />
            <div className="flex justify-end gap-2">
              <Button type="button" variant="secondary" size="sm" onClick={() => setEditing(null)}>取消</Button>
              <Button type="button" size="sm" disabled={!canEdit || !activeEdit.text.trim() || Array.from(activeEdit.text).length > 32000} onClick={() => { void onEdit(activeEdit.entryId, activeEdit.text).then((accepted) => { if (accepted) setEditing(null); }); }}>提交编辑</Button>
            </div>
          </div>
        ) : (
          <Bubble variant="default" align="end"><BubbleContent className="whitespace-pre-wrap">{request}</BubbleContent></Bubble>
        )}
        {activeEdit === null && nodeId !== undefined && (
          <div className="flex justify-end gap-2">
            <Button type="button" variant="secondary" size="sm" disabled={!canEdit} onClick={() => setEditing({ entryId: nodeId, text: request })}>编辑</Button>
            <Button type="button" variant="secondary" size="sm" disabled={!canEdit} onClick={() => onRegenerate(nodeId)}>重新生成</Button>
          </div>
        )}
        {entry.steering?.status === "pending" && <Button type="button" variant="secondary" size="sm" onClick={() => onWithdraw(roundId, entry.id)}>撤回</Button>}
        {entry.steering?.status === "discarded" && <p role="status" className="text-sm text-muted-foreground">未被本次执行消费</p>}
      </MessageContent></Message>
    );
  }
  return (
    <Message align="start"><MessageContent>
      {entry.content.map((block) => {
        if (block.type === "text") return block.text ? (
          <Bubble key={block.content_index} variant="secondary" align="start"><BubbleContent>
            <div className="min-w-0 max-w-full [&_p]:my-1 [&_ul]:list-disc [&_ol]:list-decimal [&_ul]:pl-5 [&_ol]:pl-5 [&_pre]:overflow-x-auto [&_pre]:rounded [&_pre]:bg-muted [&_pre]:p-3 [&_a]:underline"><ReactMarkdown>{block.text}</ReactMarkdown></div>
          </BubbleContent></Bubble>
        ) : null;
        if (block.type === "thinking") return block.thinking ? (
          <Collapsible key={block.content_index} defaultOpen={false} className="text-sm">
            <CollapsibleTrigger asChild><Button type="button" variant="ghost" size="sm" aria-label="思考内容" className="group">思考<ChevronDown aria-hidden className="size-4 transition-transform group-data-[state=open]:rotate-180" /></Button></CollapsibleTrigger>
            <CollapsibleContent className="whitespace-pre-wrap break-words px-3 py-2 text-muted-foreground">{block.thinking}</CollapsibleContent>
          </Collapsible>
        ) : null;
        return !entry.stop_reason ? (
          <Collapsible key={block.content_index} defaultOpen={false} className="text-sm">
            <CollapsibleTrigger asChild><Button type="button" variant="ghost" size="sm" aria-label={`${block.name} 生成中的调用参数`}>{block.name} · 生成中<ChevronDown aria-hidden className="size-4" /></Button></CollapsibleTrigger>
            <CollapsibleContent><pre className="whitespace-pre-wrap break-all">{JSON.stringify(block.arguments, null, 2)}</pre></CollapsibleContent>
          </Collapsible>
        ) : null;
      })}
      {entry.stop_reason === "length" && <p role="status" className="text-sm text-muted-foreground">输出已达到上限</p>}
      {entry.stop_reason === "aborted" && <p role="status" className="text-sm text-muted-foreground">已取消</p>}
    </MessageContent></Message>
  );
}

export default function ChatTranscript({ rounds, onRetry, onWithdraw, retrying, canEdit, onEdit, onRegenerate }: {
  rounds: ReActRound[];
  onRetry: (roundId: string, operationId: string) => void;
  onWithdraw: (roundId: string, steeringId: string) => void;
  retrying?: string;
  canEdit: boolean;
  onEdit: (entryId: string, text: string) => Promise<boolean>;
  onRegenerate: (entryId: string) => void;
}) {
  const [editing, setEditing] = useState<EditState>(null);
  return (
    <MessageScrollerProvider autoScroll defaultScrollPosition="end" scrollPreviousItemPeek={64}>
      <MessageScroller className="min-h-0 flex-1">
        <MessageScrollerViewport className="pt-10 pb-4">
          <MessageScrollerContent aria-busy={rounds.some((round) => round.status === "running")} className="mx-auto w-full max-w-4xl px-6">
            {rounds.map((round) => (
              <Fragment key={round.id}>
                {round.entries.filter((entry) => entry.kind === "tool" || (entry.kind === "user" ? !!entry.request : entry.content.some((block) => block.type === "text" ? !!block.text : block.type === "thinking" ? !!block.thinking : !entry.stop_reason) || entry.stop_reason === "length" || entry.stop_reason === "aborted")).map((entry) => (
                  <MessageScrollerItem key={entry.id} messageId={entry.id}>
                    <Entry entry={entry} roundId={round.id} onWithdraw={onWithdraw} canEdit={canEdit} editing={editing} setEditing={setEditing} onEdit={onEdit} onRegenerate={onRegenerate} />
                  </MessageScrollerItem>
                ))}
                {round.pending?.map((operation) => <MessageScrollerItem key={`pending:${operation.operation_id}`} messageId={`${round.id}:pending:${operation.operation_id}`}>
                  <div className="flex items-center gap-2">
                    <p role="status" className="text-sm text-muted-foreground">提交结果未知</p>
                    <Button type="button" variant="secondary" size="sm" disabled={retrying === operation.operation_id} onClick={() => onRetry(round.id, operation.operation_id)}>重试</Button>
                  </div>
                </MessageScrollerItem>)}
                {(round.status !== "completed" || round.error) && <MessageScrollerItem messageId={`${round.id}:status`}>
                  {round.status === "running" && <Loader2 role="status" aria-label="执行中" className="size-4 animate-spin text-muted-foreground" />}
                  {round.status === "cancelled" && <p role="status" className="text-sm text-muted-foreground">已取消</p>}
                  {round.status === "interrupted" && <p role="status" className="text-sm text-muted-foreground">已中断</p>}
                  {round.status === "failed" && !round.error && <p role="status" className="text-sm text-muted-foreground">本次运行失败</p>}
                  {round.error && round.status !== "interrupted" && <p role="alert" className="text-sm text-destructive">{round.error}</p>}
                </MessageScrollerItem>}
              </Fragment>
            ))}
          </MessageScrollerContent>
        </MessageScrollerViewport>
        <MessageScrollerButton />
      </MessageScroller>
    </MessageScrollerProvider>
  );
}
