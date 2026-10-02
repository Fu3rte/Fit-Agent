import { useEffect, useRef, useState } from "react";
import { Maximize2, Minimize2, SendHorizontal, Square } from "lucide-react";
import { canSubmitChatInput } from "../utils/reactAgent";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";

export default function ChatComposer({
  busy,
  ready,
  error,
  unknownRequests,
  onSend,
  onStop,
}: {
  busy: boolean;
  ready: boolean;
  error?: string;
  unknownRequests: string[];
  onSend: (text: string) => Promise<boolean>;
  onStop: () => void;
}) {
  const [request, setRequest] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const pending = useRef(false);
  const revision = useRef(0);
  const requestRef = useRef<HTMLTextAreaElement>(null);
  const [composerExpanded, setComposerExpanded] = useState(false);
  const [singleLine, setSingleLine] = useState(true);
  const [atMaxHeight, setAtMaxHeight] = useState(false);
  useEffect(() => {
    const element = requestRef.current;
    if (element === null) return;
    const style = window.getComputedStyle(element);
    const lineHeight = Number.parseFloat(style.lineHeight);
    const padY =
      Number.parseFloat(style.paddingTop) +
      Number.parseFloat(style.paddingBottom);
    const maxHeight = Number.parseFloat(style.maxHeight);
    setSingleLine(element.scrollHeight <= lineHeight * 1.5 + padY);
    setAtMaxHeight(
      request.trim() !== "" &&
        Number.isFinite(maxHeight) &&
        element.scrollHeight >= maxHeight - 1,
    );
  }, [request, composerExpanded]);
  const showExpand =
    composerExpanded || (request.trim() !== "" && atMaxHeight);

  const valid = canSubmitChatInput(request, unknownRequests);
  const send = () => {
    if (!ready || pending.current || !valid) return;
    const submittedRevision = revision.current;
    pending.current = true;
    setSubmitting(true);
    void onSend(request).then((accepted) => {
      pending.current = false;
      setSubmitting(false);
      if (accepted && revision.current === submittedRevision) setRequest("");
    });
  };

  return (
    <div className="shrink-0 bg-background">
      <div className="mx-auto w-full max-w-4xl px-6 pt-4 pb-8">
        {error && <p role="alert" className="mb-2 text-sm text-destructive">{error}</p>}
        {busy && (
          <div className="mb-2 flex justify-end">
            <Button type="button" size="icon" variant="secondary" aria-label="停止" onClick={onStop} className="size-8"><Square aria-hidden className="size-4" /></Button>
          </div>
        )}

        <div className="relative flex flex-col rounded-3xl bg-card px-4 py-3 shadow-md">
          {showExpand && (
            <button
              type="button"
              aria-label={composerExpanded ? "收起输入框" : "展开输入框"}
              onClick={() => setComposerExpanded((value) => !value)}
              className="absolute top-2 right-2 z-10 grid size-8 place-items-center rounded-full text-muted-foreground hover:bg-secondary hover:text-foreground"
            >
              {composerExpanded ? (
                <Minimize2 aria-hidden className="size-4" />
              ) : (
                <Maximize2 aria-hidden className="size-4" />
              )}
            </button>
          )}

          <Textarea
            ref={requestRef}
            value={request}
            onChange={(event) => { revision.current += 1; setRequest(event.target.value); }}
            onKeyDown={(event) => {
              if (
                event.key !== "Enter" ||
                event.shiftKey ||
                event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229
              ) {
                return;
              }
              event.preventDefault();
              send();
            }}
            aria-label="消息"
            rows={1}
            className={
              composerExpanded
                ? "min-h-32 max-h-[min(40vh,20rem)] w-full resize-none overflow-y-auto border-0 bg-transparent py-1 pr-10 pl-1 focus-visible:ring-0 field-sizing-content scrollbar-none [&::-webkit-scrollbar]:hidden"
                : singleLine
                  ? "min-h-0 max-h-17 w-full resize-none overflow-y-auto border-0 bg-transparent py-1 pr-14 pl-1 focus-visible:ring-0 field-sizing-content scrollbar-none [&::-webkit-scrollbar]:hidden"
                  : "min-h-0 max-h-17 w-full resize-none overflow-y-auto border-0 bg-transparent py-1 pr-10 pl-1 focus-visible:ring-0 field-sizing-content scrollbar-none [&::-webkit-scrollbar]:hidden"
            }
          />

          {request.trim() !== "" &&
            (composerExpanded || !singleLine ? (
              <div className="mt-1 flex justify-end">
                <Button
                  size="icon"
                  aria-label="发送"
                  onClick={send}
                  disabled={!ready || submitting || !valid}
                  className="size-8"
                >
                  <SendHorizontal />
                </Button>
              </div>
            ) : (
              <Button
                size="icon"
                aria-label="发送"
                onClick={send}
                disabled={!ready || submitting || !valid}
                className="absolute top-1/2 right-4 size-8 -translate-y-1/2"
              >
                <SendHorizontal />
              </Button>
            ))}
        </div>
      </div>
    </div>
  );
}
