import { useEffect, useRef, useState } from "react";
import { Maximize2, Minimize2, SendHorizontal, Square } from "lucide-react";
import { canSubmitChatInput } from "../utils/reactAgent";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import {
  AttachmentChips,
  AttachmentPicker,
  useAttachmentDrafts,
} from "./Attachments";
import type { AttachmentDraft } from "../utils/attachments";

export default function ChatComposer({
  busy,
  ready,
  error,
  unknownKeys,
  onSend,
  onStop,
  centered,
}: {
  busy: boolean;
  ready: boolean;
  error?: string;
  /** 未确认 Steering 的输入身份（文本与有序附件集合）：同身份输入只允许显式重试 */
  unknownKeys: string[];
  onSend: (text: string, attachments: AttachmentDraft[]) => Promise<boolean>;
  onStop: () => void;
  /** 空白草稿版式：输入框给出示例问法 */
  centered: boolean;
}) {
  const [request, setRequest] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const files = useAttachmentDrafts();
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
  // 附件集合的任何变化都推进代际，避免迟到的受理结果清掉发送后新挂入的附件
  useEffect(() => {
    revision.current += 1;
  }, [files.drafts]);
  const hasContent = request.trim() !== "" || files.drafts.length > 0;
  const hasTray = files.drafts.length > 0 || files.error !== null;
  const showExpand = composerExpanded || (hasContent && atMaxHeight);
  const inlineActions = !composerExpanded && singleLine && !hasTray;

  const valid = canSubmitChatInput(request, files.drafts, unknownKeys);
  const send = () => {
    if (!ready || pending.current || !valid) return;
    const submittedRevision = revision.current;
    const submitted = files.drafts;
    pending.current = true;
    setSubmitting(true);
    void onSend(request, submitted).then((accepted) => {
      pending.current = false;
      setSubmitting(false);
      if (accepted && revision.current === submittedRevision) {
        setRequest("");
        files.clear();
      }
    });
  };

  return (
    <div className="shrink-0 bg-background">
      <div className="mx-auto w-full max-w-4xl px-6 pt-4 pb-8">
        {error && (
          <p role="alert" className="mb-2 text-sm text-destructive">
            {error}
          </p>
        )}

        <div
          className="relative flex flex-col rounded-3xl bg-card px-4 py-3 shadow-md"
          onDragOver={(event) => {
            if (event.dataTransfer.types.includes("Files"))
              event.preventDefault();
          }}
          onDrop={(event) => {
            const dropped = Array.from(event.dataTransfer.files);
            if (dropped.length === 0) return;
            event.preventDefault();
            files.pick(dropped);
          }}
        >
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
            onChange={(event) => {
              revision.current += 1;
              setRequest(event.target.value);
            }}
            onKeyDown={(event) => {
              if (
                event.key !== "Enter" ||
                event.shiftKey ||
                event.nativeEvent.isComposing ||
                event.nativeEvent.keyCode === 229
              ) {
                return;
              }
              event.preventDefault();
              send();
            }}
            aria-label="消息"
            placeholder={
              centered
                ? "说说你的训练目标、当前计划或今天完成的训练"
                : undefined
            }
            rows={1}
            className={cn(
              "w-full resize-none overflow-y-auto border-0 bg-transparent py-1 focus-visible:ring-0 field-sizing-content scrollbar-none [&::-webkit-scrollbar]:hidden",
              composerExpanded
                ? "min-h-32 max-h-[min(40vh,20rem)]"
                : "min-h-0 max-h-17",
              inlineActions
                ? cn("pl-10", busy && hasContent ? "pr-24" : "pr-14")
                : "pl-1 pr-10",
            )}
          />

          <AttachmentChips
            drafts={files.drafts}
            error={files.error}
            onRemove={files.remove}
          />

          {inlineActions && (
            <div className="absolute top-1/2 left-2 flex -translate-y-1/2 items-center">
              <AttachmentPicker onPick={files.pick} />
            </div>
          )}

          {(busy || hasContent) && (
            <div
              className={
                inlineActions
                  ? "absolute top-1/2 right-4 flex -translate-y-1/2 items-center gap-1"
                  : "mt-1 flex justify-end gap-1"
              }
            >
              {busy && (
                <Button
                  type="button"
                  size="icon"
                  variant="secondary"
                  aria-label="停止"
                  onClick={onStop}
                  className="size-8"
                >
                  <Square aria-hidden className="size-4" />
                </Button>
              )}
              {hasContent && (
                <Button
                  size="icon"
                  aria-label="发送"
                  onClick={send}
                  disabled={!ready || submitting || !valid}
                  className="size-8"
                >
                  <SendHorizontal />
                </Button>
              )}
            </div>
          )}

          {!inlineActions && (
            <div className="mt-1 flex items-center">
              <AttachmentPicker onPick={files.pick} />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
