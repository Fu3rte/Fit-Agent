import { useEffect, useRef, useState } from "react";
import { Maximize2, Minimize2, SendHorizontal } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";

export default function ChatComposer({
  busy,
  onSend,
}: {
  busy: boolean;
  onSend: (text: string) => void;
}) {
  const [request, setRequest] = useState("");
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

  const send = () => {
    const text = request.trim();
    if (text === "") return;
    setRequest("");
    onSend(text);
  };

  return (
    <div className="shrink-0 bg-background">
      <div className="mx-auto w-full max-w-4xl px-6 pt-4 pb-8">
        {busy && (
          <div className="mb-2 flex items-center gap-2 rounded-lg bg-secondary px-3 py-1.5 text-xs text-secondary-foreground">
            <span
              className="size-1.5 animate-pulse rounded-full bg-current"
              aria-hidden
            />
            处理中
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
            onChange={(event) => setRequest(event.target.value)}
            onKeyDown={(event) => {
              if (
                event.key !== "Enter" ||
                event.shiftKey ||
                event.nativeEvent.isComposing
              ) {
                return;
              }
              event.preventDefault();
              if (busy) return;
              send();
            }}
            placeholder="用一句话记录训练"
            disabled={busy}
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
                  disabled={busy}
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
                disabled={busy}
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
