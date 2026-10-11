import { useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { FileText } from "lucide-react";
import { getAttachmentContent } from "@/lib/api";
import { formatAttachmentBytes } from "./Attachments";
import type { AttachmentDraft } from "../utils/attachments";

/** 内嵌附件卡片：点击按需拉取并展开原文（plan-import-adjustment-contract §4） */
function MessageAttachmentCard({
  sessionId,
  draft,
}: {
  sessionId: string;
  draft: AttachmentDraft;
}) {
  const [open, setOpen] = useState(false);
  const content = useQuery({
    queryKey: ["attachment", sessionId, draft.attachment_id],
    queryFn: ({ signal }) =>
      getAttachmentContent(sessionId, draft.attachment_id, signal),
    enabled: open,
  });
  return (
    <>
      <button
        type="button"
        aria-expanded={open}
        aria-label={`查看 ${draft.file_name} 原文`}
        onClick={() => setOpen((value) => !value)}
        className="mt-2 flex w-full items-center gap-3 rounded-xl border border-foreground/10 bg-foreground/5 p-2.5 text-left transition-colors hover:bg-foreground/10"
      >
        <span className="shrink-0 rounded-lg bg-foreground/10 p-2">
          <FileText aria-hidden className="h-4 w-4" />
        </span>
        <span className="flex min-w-0 flex-col">
          <span className="text-xs font-medium line-clamp-1">
            {draft.file_name}
          </span>
          <span className="text-[10px] opacity-80">
            {formatAttachmentBytes(draft.size_bytes)}
          </span>
        </span>
      </button>
      {open && content.isError && (
        <p role="alert" className="mt-2 text-xs text-destructive">
          {content.error.message}
        </p>
      )}
      {open && content.data !== undefined && (
        <pre className="mt-2 max-h-80 overflow-y-auto rounded-xl bg-foreground/5 p-2.5 text-xs leading-relaxed whitespace-pre-wrap break-words">
          {content.data.text}
        </pre>
      )}
    </>
  );
}

/** 用户消息气泡：文本指令与附件卡片封装在同一父级气泡内 */
export default function UserMessageBubble({
  sessionId,
  request,
  attachments,
  actions,
}: {
  sessionId: string;
  request: string;
  attachments: readonly AttachmentDraft[];
  /** 气泡悬停操作区（编辑与重新生成），锚定在气泡主体右下 */
  actions?: ReactNode;
}) {
  return (
    <div className="group/bubble relative self-end max-w-[80%] rounded-2xl rounded-tr-sm bg-bubble-out p-3 text-bubble-out-foreground shadow-sm inset-ring-1 inset-ring-bubble-out-border">
      {request !== "" && (
        <p className="text-sm leading-relaxed whitespace-pre-wrap">{request}</p>
      )}
      {attachments.map((item) => (
        <MessageAttachmentCard
          key={item.attachment_id}
          sessionId={sessionId}
          draft={item}
        />
      ))}
      {actions}
    </div>
  );
}
