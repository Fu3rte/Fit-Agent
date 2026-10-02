import { Fragment } from "react";
import { ChevronDown, Loader2 } from "lucide-react";
import ReactMarkdown from "react-markdown";
import { Bubble, BubbleContent } from "@/components/ui/bubble";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Message, MessageContent } from "@/components/ui/message";
import { MessageScroller, MessageScrollerButton, MessageScrollerContent, MessageScrollerItem, MessageScrollerProvider, MessageScrollerViewport } from "@/components/ui/message-scroller";
import ToolCallCard from "./ToolCallCard";
import type { ReActEntry, ReActRound } from "../utils/reactAgent";

function Entry({ entry }: { entry: ReActEntry }) {
  if (entry.kind === "tool") return <ToolCallCard {...entry} />;
  if (entry.kind === "user") return entry.request ? (
    <Message align="end"><MessageContent>
      <Bubble variant="default" align="end"><BubbleContent className="whitespace-pre-wrap">{entry.request}</BubbleContent></Bubble>
      {entry.steering?.status === "discarded" && <p role="status" className="text-sm text-muted-foreground">未被本次执行消费</p>}
    </MessageContent></Message>
  ) : null;
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

export default function ChatTranscript({ rounds }: { rounds: ReActRound[] }) {
  return (
    <MessageScrollerProvider autoScroll defaultScrollPosition="end" scrollPreviousItemPeek={64}>
      <MessageScroller className="min-h-0 flex-1">
        <MessageScrollerViewport className="pt-10 pb-4">
          <MessageScrollerContent aria-busy={rounds.some((round) => round.status === "running")} className="mx-auto w-full max-w-4xl px-6">
            {rounds.map((round) => (
              <Fragment key={round.id}>
                {round.entries.filter((entry) => entry.kind === "tool" || (entry.kind === "user" ? !!entry.request : entry.content.some((block) => block.type === "text" ? !!block.text : block.type === "thinking" ? !!block.thinking : !entry.stop_reason) || entry.stop_reason === "length" || entry.stop_reason === "aborted")).map((entry) => (
                  <MessageScrollerItem key={entry.id} messageId={entry.id}><Entry entry={entry} /></MessageScrollerItem>
                ))}
                {round.unknown_steering?.map((request, index) => <MessageScrollerItem key={`unknown:${index}`} messageId={`${round.id}:unknown:${index}`}><p role="status" className="text-sm text-muted-foreground">Steering 提交结果未知。</p><p className="whitespace-pre-wrap text-sm">{request}</p></MessageScrollerItem>)}
                {(round.status !== "completed" || round.error) && <MessageScrollerItem messageId={`${round.id}:status`}>
                  {round.status === "running" && <Loader2 role="status" aria-label="执行中" className="size-4 animate-spin text-muted-foreground" />}
                  {round.status === "cancelled" && <p role="status" className="text-sm text-muted-foreground">已取消</p>}
                  {round.error && <p role="alert" className="text-sm text-destructive">{round.error}</p>}
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
