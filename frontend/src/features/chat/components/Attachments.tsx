import { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Paperclip, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { getAttachmentContent } from "@/lib/api";
import {
  appendAttachmentFiles,
  type AttachmentDraft,
} from "../utils/attachments";

/** 附件原文查询（plan-import-adjustment-contract §4）：按会话与附件身份定位，读取无副作用 */
function attachmentQueryKey(sessionId: string, attachmentId: string) {
  return ["attachment", sessionId, attachmentId] as const;
}

/** 输入框与编辑框共用的附件草稿状态：追加、移除、重置与具体错误说明 */
export function useAttachmentDrafts() {
  const [drafts, setDrafts] = useState<AttachmentDraft[]>([]);
  const [error, setError] = useState<string | null>(null);
  const pick = (files: File[]) => {
    if (files.length === 0) return;
    void appendAttachmentFiles(drafts, files).then(
      (next) => {
        setDrafts(next);
        setError(null);
      },
      (failure: unknown) =>
        setError(failure instanceof Error ? failure.message : "附件读取失败。"),
    );
  };
  return {
    drafts,
    error,
    pick,
    remove: (at: number) =>
      setDrafts((list) => list.filter((_, index) => index !== at)),
    reset: (next: AttachmentDraft[]) => {
      setDrafts(next);
      setError(null);
    },
    clear: () => {
      setDrafts([]);
      setError(null);
    },
  };
}

/** 导入入口：与输入框同区的按钮，选择 UTF-8 的 `.md`／`.txt` 文件挂入附件草稿 */
export function AttachmentPicker({
  onPick,
}: {
  onPick: (files: File[]) => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  return (
    <>
      <button
        type="button"
        aria-label="导入训练计划文件"
        onClick={() => input.current?.click()}
        className="grid size-8 place-items-center rounded-full text-muted-foreground hover:bg-secondary hover:text-foreground"
      >
        <Paperclip aria-hidden className="size-4" />
      </button>
      <input
        ref={input}
        type="file"
        multiple
        accept=".md,.txt,text/markdown,text/plain"
        className="hidden"
        onChange={(event) => {
          onPick(Array.from(event.target.files ?? []));
          event.target.value = "";
        }}
      />
    </>
  );
}

/** 附件草稿条：文件名、移除入口与具体错误说明 */
export function AttachmentChips({
  drafts,
  error,
  onRemove,
}: {
  drafts: AttachmentDraft[];
  error: string | null;
  onRemove: (at: number) => void;
}) {
  if (drafts.length === 0 && error === null) return null;
  return (
    <div className="flex flex-col gap-1">
      {drafts.map((item, at) => (
        <div
          key={item.attachment_id}
          className="flex items-center gap-2 self-start rounded-full bg-secondary py-1 pr-2 pl-3 text-sm"
        >
          <span className="max-w-64 truncate">{item.file_name}</span>
          <button
            type="button"
            aria-label={`移除 ${item.file_name}`}
            onClick={() => onRemove(at)}
            className="grid size-5 shrink-0 place-items-center rounded-full text-muted-foreground hover:bg-muted hover:text-foreground"
          >
            <X aria-hidden className="size-3" />
          </button>
        </div>
      ))}
      {error !== null && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
    </div>
  );
}

/** 已提交消息的有序附件：只展示文件名，正文按附件读取接口单独取得 */
export function AttachmentList({
  sessionId,
  attachments,
}: {
  sessionId: string;
  attachments: readonly AttachmentDraft[];
}) {
  return (
    <div className="flex flex-col items-end gap-1">
      {attachments.map((item) => (
        <AttachmentReadout
          key={item.attachment_id}
          sessionId={sessionId}
          draft={item}
        />
      ))}
    </div>
  );
}

function AttachmentReadout({
  sessionId,
  draft,
}: {
  sessionId: string;
  draft: AttachmentDraft;
}) {
  const [open, setOpen] = useState(false);
  const content = useQuery({
    queryKey: attachmentQueryKey(sessionId, draft.attachment_id),
    queryFn: ({ signal }) =>
      getAttachmentContent(sessionId, draft.attachment_id, signal),
    enabled: open,
  });
  return (
    <div className="flex flex-col items-end gap-1">
      <Button
        type="button"
        variant="ghost"
        size="sm"
        aria-expanded={open}
        aria-label={`查看 ${draft.file_name} 原文`}
        className="max-w-64 truncate text-muted-foreground hover:text-foreground"
        onClick={() => setOpen((value) => !value)}
      >
        {draft.file_name}
      </Button>
      {open && content.isError && (
        <p role="alert" className="text-sm text-destructive">
          {content.error.message}
        </p>
      )}
      {open && content.data !== undefined && (
        <pre className="max-w-2xl overflow-x-auto rounded-lg bg-secondary p-3 text-sm whitespace-pre-wrap break-words">
          {content.data.text}
        </pre>
      )}
    </div>
  );
}
