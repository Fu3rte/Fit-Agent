import { Fragment, type ReactNode } from "react";
import { Loader2 } from "lucide-react";
import ReactMarkdown from "react-markdown";
import { Bubble, BubbleContent } from "@/components/ui/bubble";
import { Message, MessageContent } from "@/components/ui/message";
import {
  MessageScroller,
  MessageScrollerButton,
  MessageScrollerContent,
  MessageScrollerItem,
  MessageScrollerProvider,
  MessageScrollerViewport,
} from "@/components/ui/message-scroller";
import { Marker, MarkerContent, MarkerIcon } from "@/components/ui/marker";
import {
  eventText,
  interruptedNotice,
  type ChatRound,
} from "@/features/chat/utils/chatRound";

function isSettled(round: ChatRound): boolean {
  if (
    round.run_status !== null &&
    round.run_status !== "pending" &&
    round.run_status !== "running"
  )
    return true;
  return round.events.some(
    (event) =>
      event.event === "done" ||
      event.event === "waiting" ||
      event.event === "error",
  );
}

/** 消息列：MessageScroller 管滚动与锚点；``children`` 须为 MessageScrollerItem */
export default function ChatTranscript({
  rounds,
  children,
}: {
  rounds: ChatRound[];
  children?: ReactNode;
}) {
  return (
    <MessageScrollerProvider
      autoScroll
      defaultScrollPosition="end"
      scrollPreviousItemPeek={64}
    >
      <MessageScroller className="min-h-0 flex-1">
        <MessageScrollerViewport className="pt-10 pb-4">
          <MessageScrollerContent
            aria-busy={rounds.some((round) => !isSettled(round)) || undefined}
            className="mx-auto w-full max-w-4xl px-6"
          >
            {rounds.map((round) => {
              // 落库的 Assistant 投影优先（含失败／部分／中止文本）；页面在途轮次只有流式 message 事件
              const assistantLines =
                round.assistants.length > 0
                  ? round.assistants.map((assistant) => assistant.content)
                  : round.events
                      .filter((event) => event.event === "message")
                      .map((event) => eventText(event));
              const notice = interruptedNotice(round);
              const settled = isSettled(round);
              return (
                <Fragment key={round.conversation_id}>
                  <MessageScrollerItem
                    messageId={`${round.conversation_id}:user`}
                    scrollAnchor
                  >
                    <Message align="end">
                      <MessageContent>
                        <Bubble variant="default" align="end">
                          <BubbleContent>{round.request}</BubbleContent>
                        </Bubble>
                      </MessageContent>
                    </Message>
                  </MessageScrollerItem>

                  {(assistantLines.length > 0 ||
                    !settled ||
                    notice !== undefined) && (
                    <MessageScrollerItem
                      messageId={`${round.conversation_id}:assistant`}
                    >
                      <Message align="start">
                        <MessageContent>
                          {assistantLines.length === 0 && !settled && (
                            <Marker role="status" className="w-auto">
                              <MarkerIcon>
                                <Loader2 className="size-4 animate-spin" />
                              </MarkerIcon>
                              <MarkerContent>正在思考…</MarkerContent>
                            </Marker>
                          )}
                          {assistantLines.length > 0 && (
                            <Bubble variant="secondary" align="start">
                              <BubbleContent className="flex flex-col gap-1">
                                {assistantLines.map((line, lineIndex) => (
                                  <div
                                    key={lineIndex}
                                    className="min-w-0 max-w-full [&>*:first-child]:mt-0 [&>*:last-child]:mb-0 [&_p]:my-1 [&_ul]:my-1 [&_ol]:my-1 [&_ul]:list-disc [&_ol]:list-decimal [&_ul]:pl-5 [&_ol]:pl-5 [&_pre]:my-2 [&_pre]:max-w-full [&_pre]:overflow-x-auto [&_pre]:rounded [&_pre]:bg-muted [&_pre]:p-3 [&_code]:break-words [&_a]:underline"
                                  >
                                    <ReactMarkdown>{line}</ReactMarkdown>
                                  </div>
                                ))}
                              </BubbleContent>
                            </Bubble>
                          )}
                          {notice !== undefined && (
                            <Marker role="status" className="mt-1 w-auto">
                              <MarkerContent>{notice}</MarkerContent>
                            </Marker>
                          )}
                        </MessageContent>
                      </Message>
                    </MessageScrollerItem>
                  )}
                </Fragment>
              );
            })}

            {children}
          </MessageScrollerContent>
        </MessageScrollerViewport>
        <MessageScrollerButton />
      </MessageScroller>
    </MessageScrollerProvider>
  );
}
