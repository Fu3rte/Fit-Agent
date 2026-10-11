import { useEffect, useRef, useState } from "react";
import { ArrowUp, Maximize2, Minimize2, Square } from "lucide-react";
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
  /** 发送期间文本的改动代际：仅在文本未变时清空，保护迟到的受理结果不吞掉新输入 */
  const requestRevision = useRef(0);
  const requestRef = useRef<HTMLTextAreaElement>(null);
  const [composerExpanded, setComposerExpanded] = useState(false);
  const [atMaxHeight, setAtMaxHeight] = useState(false);
  useEffect(() => {
    const element = requestRef.current;
    if (element === null) return;
    const style = window.getComputedStyle(element);
    const maxHeight = Number.parseFloat(style.maxHeight);
    setAtMaxHeight(
      request.trim() !== "" &&
        Number.isFinite(maxHeight) &&
        element.scrollHeight >= maxHeight - 1,
    );
  }, [request, composerExpanded]);
  const hasContent = request.trim() !== "" || files.drafts.length > 0;
  const showExpand = composerExpanded || (hasContent && atMaxHeight);

  const canSubmit =
    ready &&
    !submitting &&
    !files.pending &&
    canSubmitChatInput(request, files.drafts, unknownKeys);
  const send = () => {
    // 导入读取期间不提交：等待用户选中的附件处理完成后才提交准确集合
    if (!canSubmit || pending.current) return;
    const submittedRevision = requestRevision.current;
    const submitted = files.drafts;
    pending.current = true;
    setSubmitting(true);
    void onSend(request, submitted).then((accepted) => {
      pending.current = false;
      setSubmitting(false);
      if (!accepted) return;
      // 仅移除本次提交的附件身份，提交期间新挂入的附件保持原值
      files.discard(submitted);
      if (requestRevision.current === submittedRevision) setRequest("");
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

        <form
          onSubmit={(event) => {
            event.preventDefault();
            send();
          }}
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
          className="relative rounded-2xl border border-input bg-background p-2 shadow-sm"
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

          <AttachmentChips
            drafts={files.drafts}
            error={files.error}
            onRemove={files.remove}
          />

          <Textarea
            ref={requestRef}
            value={request}
            onChange={(event) => {
              requestRevision.current += 1;
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
              "w-full resize-none overflow-y-auto border-0 bg-transparent px-2 text-sm shadow-none focus-visible:outline-none focus-visible:ring-0 field-sizing-content",
              composerExpanded
                ? "min-h-32 max-h-[min(40vh,20rem)]"
                : "min-h-[44px] max-h-[200px]",
            )}
          />

          <div className="flex items-center justify-between px-1 pt-1">
            <AttachmentPicker onPick={files.pick} />
            <div className="flex items-center gap-1">
              {busy && (
                <Button
                  type="button"
                  size="icon"
                  variant="secondary"
                  aria-label="停止"
                  onClick={onStop}
                  className="h-8 w-8 rounded-full"
                >
                  <Square aria-hidden className="h-4 w-4" />
                </Button>
              )}
              <Button
                type="submit"
                size="icon"
                aria-label="发送"
                disabled={!canSubmit}
                className="h-8 w-8 rounded-full"
              >
                <ArrowUp aria-hidden className="h-4 w-4" />
              </Button>
            </div>
          </div>
        </form>
      </div>
    </div>
  );
}
