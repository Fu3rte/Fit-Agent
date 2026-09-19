import { Fragment, type ReactNode } from "react";
import { Loader2 } from "lucide-react";
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
import { eventText, type ChatRound } from "@/features/chat/utils/chatRound";

function isSettled(round: ChatRound): boolean {
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
        <MessageScrollerViewport className="pt-4 pb-4">
          <MessageScrollerContent
            aria-busy={rounds.some((round) => !isSettled(round)) || undefined}
            className="mx-auto w-full px-6"
          >
            {rounds.map((round) => {
              const messages = round.events.filter(
                (event) => event.event === "message",
              );
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

                  <MessageScrollerItem
                    messageId={`${round.conversation_id}:assistant`}
                  >
                    <Message align="start">
                      <MessageContent>
                        {messages.length === 0 && !settled ? (
                          <Marker role="status" className="w-auto">
                            <MarkerIcon>
                              <Loader2 className="size-4 animate-spin" />
                            </MarkerIcon>
                            <MarkerContent>正在思考…</MarkerContent>
                          </Marker>
                        ) : (
                          <Bubble variant="secondary" align="start">
                            <BubbleContent className="flex flex-col gap-1">
                              {messages.map((event, messageIndex) => (
                                <p key={messageIndex}>{eventText(event)}</p>
                              ))}
                            </BubbleContent>
                          </Bubble>
                        )}
                      </MessageContent>
                    </Message>
                  </MessageScrollerItem>
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
