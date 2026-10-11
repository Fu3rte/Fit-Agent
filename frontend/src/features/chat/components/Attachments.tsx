import { useRef, useState } from "react";
import { FileText, Paperclip, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  mergeAttachmentDrafts,
  readAttachmentFiles,
  type AttachmentDraft,
} from "../utils/attachments";

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

/** 附件字节数的展示口径：按量级换算 B／KB／MB（Intl unit:byte 无二进制进位，实测不可用） */
export function formatAttachmentBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
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
      <Button
        type="button"
        variant="ghost"
        size="icon"
        aria-label="导入训练计划文件"
        disabled={disabled}
        onClick={() => input.current?.click()}
        className="h-8 w-8 rounded-lg text-muted-foreground hover:text-foreground"
      >
        <Paperclip aria-hidden className="h-4 w-4" />
      </Button>
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

/** 附件草稿条：文件图标、文件名、字节数、移除入口与具体错误说明 */
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
      {drafts.length > 0 && (
        <div className="flex flex-wrap gap-2 px-2 pb-2">
          {drafts.map((item, at) => (
            <div
              key={item.attachment_id}
              className="flex items-center gap-2 rounded-lg border bg-muted/50 px-2.5 py-1.5 text-xs text-foreground"
            >
              <FileText
                aria-hidden
                className="h-3.5 w-3.5 shrink-0 text-muted-foreground"
              />
              <span className="max-w-[180px] truncate">{item.file_name}</span>
              <span className="shrink-0 text-muted-foreground">
                {formatAttachmentBytes(item.size_bytes)}
              </span>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label={`移除 ${item.file_name}`}
                disabled={disabled}
                onClick={() => onRemove(at)}
                className="h-5 w-5 shrink-0 rounded-md text-muted-foreground hover:bg-muted hover:text-foreground"
              >
                <X aria-hidden className="h-3 w-3" />
              </Button>
            </div>
          ))}
        </div>
      )}
      {error !== null && (
        <p role="alert" className="px-2 text-sm text-destructive">
          {error}
        </p>
      )}
    </div>
  );
}
