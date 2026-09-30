import { Fragment } from "react";
import { Loader2 } from "lucide-react";
import ReactMarkdown from "react-markdown";
import { Bubble, BubbleContent } from "@/components/ui/bubble";
import { Message, MessageContent } from "@/components/ui/message";
import { MessageScroller, MessageScrollerButton, MessageScrollerContent, MessageScrollerItem, MessageScrollerProvider, MessageScrollerViewport } from "@/components/ui/message-scroller";
import { Marker, MarkerContent, MarkerIcon } from "@/components/ui/marker";
import ToolCallCard from "./ToolCallCard";
import type { ReActRound } from "../utils/reactAgent";

export default function ChatTranscript({ rounds }: { rounds: ReActRound[] }) {
  return (
    <MessageScrollerProvider autoScroll defaultScrollPosition="end" scrollPreviousItemPeek={64}>
      <MessageScroller className="min-h-0 flex-1">
        <MessageScrollerViewport className="pt-10 pb-4">
          <MessageScrollerContent aria-busy={rounds.some((round) => round.status === "running")} className="mx-auto w-full max-w-4xl px-6">
            {rounds.map((round) => (
              <Fragment key={round.id}>
                <MessageScrollerItem messageId={`${round.id}:user`} scrollAnchor>
                  <Message align="end"><MessageContent><Bubble variant="default" align="end"><BubbleContent className="whitespace-pre-wrap">{round.request}</BubbleContent></Bubble></MessageContent></Message>
                </MessageScrollerItem>
                {round.tools.map(({ id, ...props }) => (
                  <MessageScrollerItem key={id} messageId={id}><ToolCallCard {...props} /></MessageScrollerItem>
                ))}
                <MessageScrollerItem messageId={`${round.id}:assistant`}>
                  <Message align="start"><MessageContent>
                    {round.text !== undefined && (
                      <Bubble variant="secondary" align="start"><BubbleContent>
                        <div className="min-w-0 max-w-full [&_p]:my-1 [&_ul]:list-disc [&_ol]:list-decimal [&_ul]:pl-5 [&_ol]:pl-5 [&_pre]:overflow-x-auto [&_pre]:rounded [&_pre]:bg-muted [&_pre]:p-3 [&_a]:underline">
                          <ReactMarkdown>{round.text}</ReactMarkdown>
                        </div>
                      </BubbleContent></Bubble>
                    )}
                    {round.status === "running" && <Marker role="status" className="w-auto"><MarkerIcon><Loader2 className="size-4 animate-spin" /></MarkerIcon><MarkerContent>正在执行…</MarkerContent></Marker>}
                    {round.error && <p role="alert" className="mt-2 text-sm text-destructive">{round.error}</p>}
                  </MessageContent></Message>
                </MessageScrollerItem>
              </Fragment>
            ))}
          </MessageScrollerContent>
        </MessageScrollerViewport>
        <MessageScrollerButton />
      </MessageScroller>
    </MessageScrollerProvider>
  );
}
