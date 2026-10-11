import { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Paperclip, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { getAttachmentContent } from "@/lib/api";
import {
  mergeAttachmentDrafts,
  readAttachmentFiles,
  type AttachmentDraft,
} from "../utils/attachments";

/** 附件原文查询（plan-import-adjustment-contract §4）：按会话与附件身份定位，读取无副作用 */
function attachmentQueryKey(sessionId: string, attachmentId: string) {
  return ["attachment", sessionId, attachmentId] as const;
}

/** 一次导入读取任务（附件契约 §1）：按发起顺序排队，完成或失败后就地合入或整批拒绝 */
interface ImportTask {
  prepared?: AttachmentDraft[];
  failure?: string;
  settled: boolean;
}

/**
 * 输入框与编辑框共用的附件草稿状态：追加、移除、重置与具体错误说明。
 * 以 ref 作为最新状态基准，导入任务按发起顺序排队合入，避免迟到的读取结果覆盖期间的移除、
 * 清空与重置；代际变化（clear／reset／取消编辑／切换编辑对象／卸载）使在途任务整体失效。
 */
export function useAttachmentDrafts() {
  const [state, setState] = useState<{
    drafts: AttachmentDraft[];
    error: string | null;
    pending: boolean;
  }>({ drafts: [], error: null, pending: false });
  const draftsRef = useRef<AttachmentDraft[]>([]);
  const errorRef = useRef<string | null>(null);
  const importsRef = useRef<ImportTask[]>([]);

  /** 由最新草稿、错误与在途任务数发布一次状态，保证渲染读到的都是同步基准 */
  const publish = () => {
    setState({
      drafts: draftsRef.current,
      error: errorRef.current,
      pending: importsRef.current.length > 0,
    });
  };

  /**
   * 按发起顺序推进已完成的导入：读取中的批次阻断其后批次，保证最终顺序与用户发起顺序一致；
   * 每个批次独立读取，并入时按当时最新集合校验字节上限，失败保持当前有效集合完整。
   */
  const flush = () => {
    const queue = importsRef.current;
    let drafts = draftsRef.current;
    let error = errorRef.current;
    while (queue.length > 0 && queue[0].settled) {
      const task = queue.shift() as ImportTask;
      if (task.failure !== undefined) {
        error = task.failure;
        continue;
      }
      try {
        drafts = mergeAttachmentDrafts(drafts, task.prepared ?? []);
      } catch (failure) {
        error =
          failure instanceof Error ? failure.message : "附件读取失败。";
      }
    }
    draftsRef.current = drafts;
    errorRef.current = error;
    publish();
  };

  const pick = (files: File[]) => {
    if (files.length === 0) return;
    // 与在途导入无重叠的新操作清除上一轮错误；同一并发轮内失败说明由失败批次保留
    if (importsRef.current.length === 0) errorRef.current = null;
    const task: ImportTask = { settled: false };
    importsRef.current.push(task);
    publish();
    void readAttachmentFiles(files).then(
      (prepared) => {
        task.prepared = prepared;
        task.settled = true;
        flush();
      },
      (failure: unknown) => {
        task.failure =
          failure instanceof Error ? failure.message : "附件读取失败。";
        task.settled = true;
        flush();
      },
    );
  };

  const invalidate = () => {
    importsRef.current = [];
  };

  return {
    drafts: state.drafts,
    error: state.error,
    /** 存在尚未读取完成的导入：提交发送与编辑需等待，避免提交不完整集合 */
    pending: state.pending,
    pick,
    remove: (at: number) => {
      draftsRef.current = draftsRef.current.filter(
        (_, index) => index !== at,
      );
      publish();
    },
    /** 移除已提交集合中的附件：仅按身份剔除，提交期间新挂入的附件保持原值 */
    discard: (discarded: readonly AttachmentDraft[]) => {
      const removed = new Set(discarded.map((item) => item.attachment_id));
      draftsRef.current = draftsRef.current.filter(
        (item) => !removed.has(item.attachment_id),
      );
      publish();
    },
    reset: (next: AttachmentDraft[]) => {
      invalidate();
      draftsRef.current = [...next];
      errorRef.current = null;
      publish();
    },
    clear: () => {
      invalidate();
      draftsRef.current = [];
      errorRef.current = null;
      publish();
    },
  };
}

/** 导入入口：与输入框同区的按钮，选择 UTF-8 的 `.md`／`.txt` 文件挂入附件草稿 */
export function AttachmentPicker({
  onPick,
  disabled,
}: {
  onPick: (files: File[]) => void;
  disabled?: boolean;
}) {
  const input = useRef<HTMLInputElement>(null);
  return (
    <>
      <button
        type="button"
        aria-label="导入训练计划文件"
        disabled={disabled}
        onClick={() => input.current?.click()}
        className="grid size-8 place-items-center rounded-full text-muted-foreground hover:bg-secondary hover:text-foreground disabled:pointer-events-none disabled:opacity-50"
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
  disabled,
}: {
  drafts: AttachmentDraft[];
  error: string | null;
  onRemove: (at: number) => void;
  disabled?: boolean;
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
            disabled={disabled}
            onClick={() => onRemove(at)}
            className="grid size-5 shrink-0 place-items-center rounded-full text-muted-foreground hover:bg-muted hover:text-foreground disabled:pointer-events-none disabled:opacity-50"
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
