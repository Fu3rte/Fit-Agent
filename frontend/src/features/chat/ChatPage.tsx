/**
 * 对话页（/）：居中消息流 + 草稿卡闭环。
 * 数据全部经 src/lib/api.ts 走 mock 中间件（真实 fetch + 原生 EventSource，
 * 客户端代码为将来直连后端的形状）。SSE 事件见 src/lib/contract.ts 契约草案。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  AlertCircle,
  CircleStop,
  SendHorizonal,
  Settings,
  Sparkles,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  cancelRun,
  confirmDraft,
  createRun,
  createSession,
  getMessages,
  getProvider,
  getSessions,
  recalcDraft,
  type ApiError,
} from "@/lib/api";
import type {
  ChatMessage,
  Draft,
  DraftPayload,
  FieldDiff,
  SseEvent,
} from "@/lib/contract";
import { DraftCard } from "./DraftCard";
import { Markdown } from "./Markdown";
import { useChatEvents } from "./useChatEvents";

/** Run 完成后需失效的数据类 query（records/profile/stats/review 由各看板页消费） */
const DATA_QUERY_KEYS = ["records", "stats", "profile", "review"] as const;

/** api.ts 抛出的是 Error & Partial<ApiError>；安全取回机器错误信息 */
const toApiError = (error: unknown): ApiError => {
  const err = error as Partial<ApiError> | null;
  return {
    http_status: err?.http_status ?? 0,
    error_code: err?.error_code ?? "invalid_request",
    message: err?.message ?? "请求失败",
    detail: err?.detail,
  };
};

interface ActiveRun {
  runId: string;
  /** 发起 run 的会话；流式气泡只在该会话内展示 */
  session: string;
  userText: string;
  text: string;
  drafts: Draft[];
  compacted: boolean;
}

type Item =
  | { kind: "message"; message: ChatMessage }
  | { kind: "user"; text: string }
  | { kind: "compacted" }
  | { kind: "stream"; text: string; drafts: Draft[] }
  | { kind: "error"; text: string };

export default function ChatPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const [params] = useSearchParams();

  /* ------------------------------ 会话与数据 ------------------------------ */
  const sessions = useQuery({ queryKey: ["sessions"], queryFn: getSessions });
  // 无 ?s= 参数时默认第一个会话（不改写 URL，侧栏高亮以显式选择为准）
  const sessionId = params.get("s") ?? sessions.data?.[0]?.id ?? null;

  const provider = useQuery({ queryKey: ["provider"], queryFn: getProvider });
  const configured = provider.data?.has_api_key === true;

  const messages = useQuery({
    queryKey: ["messages", sessionId],
    queryFn: () => getMessages(sessionId as string),
    enabled: sessionId !== null,
  });

  /* -------------------------------- 本地态 -------------------------------- */
  const [input, setInput] = useState("");
  const [active, setActive] = useState<ActiveRun | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  /** 草稿缓存：draft.id -> Draft（run 流内提出 + 确认/重算后同步） */
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  /** 内联纠错：draft.id -> 纠错后的 payload（只改待确认草稿，不自动提交） */
  const [edits, setEdits] = useState<Record<string, DraftPayload>>({});
  /** 确认遇到 409 draft_stale 的草稿 */
  const [staleIds, setStaleIds] = useState<ReadonlySet<string>>(new Set());
  /** 一键重算后旧草稿 -> 新草稿 */
  const [replacedBy, setReplacedBy] = useState<Record<string, string>>({});
  /** 重算新草稿 -> 新旧草稿 Diff */
  const [recalcDiffs, setRecalcDiffs] = useState<Record<string, FieldDiff[]>>(
    {},
  );

  const sessionIdRef = useRef(sessionId);
  const activeRef = useRef<ActiveRun | null>(null);
  useEffect(() => {
    sessionIdRef.current = sessionId;
  }, [sessionId]);
  useEffect(() => {
    activeRef.current = active;
  }, [active]);

  /* ------------------------------- 查询失效 ------------------------------- */
  const invalidateData = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["messages"] });
    void queryClient.invalidateQueries({ queryKey: ["sessions"] });
    for (const key of DATA_QUERY_KEYS)
      void queryClient.invalidateQueries({ queryKey: [key] });
  }, [queryClient]);

  /* ------------------------------- SSE 处理 ------------------------------- */
  const handleEvent = useCallback(
    (ev: SseEvent) => {
      const mine = (runId: string) => {
        const cur = activeRef.current;
        // runId === ""：POST /api/runs 尚未返回时的乐观窗口，当前仅可能是我方 run
        return cur !== null && (cur.runId === runId || cur.runId === "");
      };
      switch (ev.event) {
        case "run.started":
          setActive((prev) =>
            prev && prev.runId === "" ? { ...prev, runId: ev.run_id } : prev,
          );
          break;
        case "message.delta":
          setActive((prev) =>
            prev && mine(ev.run_id)
              ? { ...prev, text: prev.text + ev.text }
              : prev,
          );
          break;
        case "draft.proposed": {
          setDrafts((prev) => ({ ...prev, [ev.draft.id]: ev.draft }));
          setActive((prev) =>
            prev && mine(ev.run_id)
              ? { ...prev, drafts: [...prev.drafts, ev.draft] }
              : prev,
          );
          break;
        }
        case "context.compacted":
          // B3：上下文压缩仅界面轻提示，不做轨迹面板
          setActive((prev) =>
            prev && mine(ev.run_id) ? { ...prev, compacted: true } : prev,
          );
          break;
        case "run.completed":
        case "run.cancelled":
          if (mine(ev.run_id)) {
            setActive(null);
            activeRef.current = null;
            invalidateData();
          }
          break;
        case "run.failed":
          if (mine(ev.run_id)) {
            setActive(null);
            activeRef.current = null;
            setRunError(`本次执行失败（${ev.error_code}），请重试。`);
            invalidateData();
          }
          break;
        case "error":
          if (ev.error_code === "conversation_busy") {
            toast.error("对话进行中", { description: ev.message });
          } else if (ev.error_code === "not_configured") {
            void queryClient.invalidateQueries({ queryKey: ["provider"] });
          } else {
            toast.error(ev.message);
          }
          break;
      }
    },
    [invalidateData, queryClient],
  );
  useChatEvents(handleEvent);

  /* -------------------------------- 发送 ---------------------------------- */
  const send = async () => {
    const text = input.trim();
    if (!text || !sessionId || !configured) return;
    // 运行中不做乐观覆盖：覆盖会以 runId:"" 顶掉原 run，致其 delta 串扰进新流或被丢弃。
    // 空闲时才挂乐观 active；运行中再发送由 409 conversation_busy 走全局 toast（演示路径）。
    const wasIdle = activeRef.current === null;
    if (wasIdle) {
      setInput("");
      setRunError(null);
      setActive({
        runId: "",
        session: sessionId,
        userText: text,
        text: "",
        drafts: [],
        compacted: false,
      });
    }
    try {
      await createRun({
        session_id: sessionId,
        message: text,
        client_request_id: crypto.randomUUID(),
      });
    } catch (error) {
      if (wasIdle) {
        setActive(null);
        activeRef.current = null;
      }
      const err = toApiError(error);
      if (err.error_code === "conversation_busy") {
        // A5：conversation_busy 走全局 toast
        toast.error("已有正在进行的对话", { description: err.message });
      } else if (err.error_code === "not_configured") {
        void queryClient.invalidateQueries({ queryKey: ["provider"] });
        toast.error("尚未配置可用模型", { description: err.message });
      } else {
        toast.error(err.message ?? "发送失败");
      }
      if (wasIdle) setInput(text); // 保留输入便于重试；busy 时未清空，无需恢复
    }
  };

  const cancel = useMutation({
    mutationFn: (runId: string) => cancelRun(runId),
    onSuccess: (res) => {
      if (res.status === "cancelled") toast.success("已取消当前任务");
    },
    onError: (error) => toast.error(toApiError(error).message),
  });

  /* ------------------------------ 草稿操作 -------------------------------- */
  const confirm = useMutation({
    mutationFn: (draft: Draft) =>
      confirmDraft(draft.id, edits[draft.id] ?? draft.payload),
    onSuccess: (result) => {
      if (result.newly_committed) {
        toast.success("已采纳", { description: result.summary });
        setDrafts((prev) => ({
          ...prev,
          [result.draft_id]: { ...prev[result.draft_id], status: "committed" },
        }));
        invalidateData();
      } else {
        toast.info("该草稿已提交过（幂等返回原结果）");
      }
    },
    onError: (error, draft) => {
      const err = toApiError(error);
      if (err.error_code === "draft_stale") {
        setStaleIds((prev) => new Set(prev).add(draft.id));
        toast.error("草稿已过期", { description: err.message });
      } else {
        toast.error(err.message ?? "确认失败");
      }
    },
  });

  const recalc = useMutation({
    mutationFn: (draftId: string) => recalcDraft(draftId),
    onSuccess: (result) => {
      setDrafts((prev) => ({
        ...prev,
        [result.new_draft.id]: result.new_draft,
        [result.old_draft.id]: result.old_draft,
      }));
      setStaleIds((prev) => {
        const next = new Set(prev);
        next.delete(result.old_draft.id);
        return next;
      });
      setReplacedBy((prev) => ({
        ...prev,
        [result.old_draft.id]: result.new_draft.id,
      }));
      setRecalcDiffs((prev) => ({
        ...prev,
        [result.new_draft.id]: result.draft_vs_draft_diff,
      }));
      toast.success("已按最新数据重算，请再次确认新草稿");
    },
    onError: (error) => toast.error(toApiError(error).message),
  });

  /* ----------------------------- 外部跳转预填 ------------------------------ */
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    const state = location.state as { prefill?: unknown } | null;
    if (state && typeof state.prefill === "string") {
      setInput(state.prefill);
      textareaRef.current?.focus();
      // 用后清除 state，避免返回/刷新重复预填
      navigate(location.pathname + location.search, { replace: true });
    }
  }, [location.state, location.pathname, location.search, navigate]);

  /* -------------------------------- 渲染 ---------------------------------- */
  const resolveDraftId = (id: string): string => {
    let cur = id;
    while (replacedBy[cur]) cur = replacedBy[cur];
    return cur;
  };

  const renderDraftCard = (draftOrId: Draft | string) => {
    const id = typeof draftOrId === "string" ? draftOrId : draftOrId.id;
    const resolvedId = resolveDraftId(id);
    const draft =
      typeof draftOrId === "string" ? drafts[resolvedId] : draftOrId;
    if (!draft) {
      return (
        <p className="mt-2 text-xs text-muted-foreground">
          草稿详情仅在当前页面会话内保留（mock 阶段已知限制）。
        </p>
      );
    }
    return (
      <DraftCard
        draft={draft}
        payload={edits[draft.id] ?? draft.payload}
        onChange={(p) => setEdits((prev) => ({ ...prev, [draft.id]: p }))}
        onConfirm={() => confirm.mutate(draft)}
        onRecalc={() => recalc.mutate(draft.id)}
        confirmPending={confirm.isPending && confirm.variables?.id === draft.id}
        recalcPending={recalc.isPending && recalc.variables === resolvedId}
        staleError={staleIds.has(draft.id)}
        recalcDiff={recalcDiffs[draft.id]}
        superseded={
          replacedBy[draft.id] !== undefined || draft.status === "stale"
        }
      />
    );
  };

  const items: Item[] = [
    ...(messages.data ?? []).map(
      (message) =>
        ({
          kind: "message",
          message,
        }) as Item,
    ),
  ];
  if (active && active.session === sessionId) {
    items.push({ kind: "user", text: active.userText });
    if (active.compacted) items.push({ kind: "compacted" });
    items.push({ kind: "stream", text: active.text, drafts: active.drafts });
  }
  if (runError) items.push({ kind: "error", text: runError });

  const scrollRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages.data, active?.text, active?.drafts.length, active?.compacted]);

  const composerDisabled = !configured || sessionId === null;
  const showActiveBar = active !== null;

  const createSessionMutation = useMutation({
    mutationFn: () => createSession(),
    onSuccess: (s) => {
      void queryClient.invalidateQueries({ queryKey: ["sessions"] });
      navigate(`/?s=${s.id}`);
    },
  });

  return (
    <div className="mx-auto flex h-full w-full max-w-3xl flex-col px-6">
      <header className="pt-10 pb-4">
        <h2 className="font-display text-3xl font-light tracking-tight">
          对话
        </h2>
        <p className="mt-1 text-sm text-muted-foreground">
          所有业务变更均由对话发起，确认后才会生效
        </p>
      </header>

      {/* 未配置模型：配置指引空状态（PRD 5.1） */}
      {provider.data && !configured ? (
        <div className="flex flex-1 items-center justify-center pb-6">
          <div className="max-w-md rounded-xl border bg-card p-6 text-center shadow-sm">
            <Settings
              className="mx-auto size-8 text-muted-foreground"
              aria-hidden
            />
            <h3 className="mt-3 font-display text-lg font-light tracking-tight">
              尚未配置可用模型
            </h3>
            <p className="mt-2 text-sm text-muted-foreground">
              配置 Provider 与 API Key
              后即可开始对话：建档、生成计划、打卡与复盘均由对话发起。
              未配置前其他 Agent 功能不可使用。
            </p>
            <Button className="mt-4" onClick={() => navigate("/settings")}>
              去设置
            </Button>
          </div>
        </div>
      ) : (
        <>
          {/* 消息流 */}
          <div
            ref={scrollRef}
            className="flex-1 space-y-4 overflow-y-auto pb-4"
          >
            {messages.isLoading && (
              <p className="pt-6 text-center text-xs text-muted-foreground">
                加载中…
              </p>
            )}
            {configured && sessionId === null && (
              <div className="flex h-full items-center justify-center">
                <div className="rounded-xl border bg-card p-6 text-center shadow-sm">
                  <Sparkles
                    className="mx-auto size-6 text-muted-foreground"
                    aria-hidden
                  />
                  <p className="mt-2 text-sm text-muted-foreground">
                    还没有会话
                  </p>
                  <Button
                    size="sm"
                    className="mt-3"
                    onClick={() => createSessionMutation.mutate()}
                    disabled={createSessionMutation.isPending}
                  >
                    新建对话
                  </Button>
                </div>
              </div>
            )}
            {items.length === 0 &&
              configured &&
              sessionId !== null &&
              !messages.isLoading && (
                <p className="pt-6 text-center text-xs text-muted-foreground">
                  开始你的第一条消息：打卡、调整计划或提问…
                </p>
              )}
            {items.map((item, i) => {
              switch (item.kind) {
                case "message":
                  return item.message.role === "user" ? (
                    <div key={item.message.id} className="flex justify-end">
                      <div className="max-w-[85%] rounded-xl border border-bubble-out-border bg-bubble-out px-4 py-3 text-sm whitespace-pre-wrap text-bubble-out-foreground">
                        {item.message.content}
                      </div>
                    </div>
                  ) : (
                    <div key={item.message.id} className="flex justify-start">
                      <div className="w-full max-w-[85%] rounded-xl border bg-bubble-in px-4 py-3 text-sm text-bubble-in-foreground">
                        <Markdown text={item.message.content} />
                        {renderDraftCard(item.message.draft_id ?? "")}
                      </div>
                    </div>
                  );
                case "user":
                  return (
                    <div key={`u-${i}`} className="flex justify-end">
                      <div className="max-w-[85%] rounded-xl border border-bubble-out-border bg-bubble-out px-4 py-3 text-sm whitespace-pre-wrap text-bubble-out-foreground">
                        {item.text}
                      </div>
                    </div>
                  );
                case "compacted":
                  return (
                    <p
                      key={`c-${i}`}
                      className="text-center text-[11px] text-muted-foreground"
                    >
                      已发生上下文压缩，核心事实已保留
                    </p>
                  );
                case "stream":
                  return (
                    <div key={`s-${i}`} className="flex justify-start">
                      <div className="w-full max-w-[85%] rounded-xl border bg-bubble-in px-4 py-3 text-sm text-bubble-in-foreground">
                        {item.text ? (
                          <Markdown text={item.text} />
                        ) : (
                          <p className="animate-pulse text-xs text-muted-foreground">
                            正在思考…
                          </p>
                        )}
                        {item.drafts.map((d) => renderDraftCard(d))}
                      </div>
                    </div>
                  );
                case "error":
                  return (
                    <div key={`e-${i}`} className="flex justify-start">
                      <div className="flex items-center gap-2 rounded-xl border border-destructive/40 bg-destructive/10 px-4 py-2.5 text-sm text-destructive">
                        <AlertCircle className="size-4" aria-hidden />
                        {item.text}
                      </div>
                    </div>
                  );
              }
            })}
          </div>

          {/* 运行中状态条 + 停止（输入不禁用：conversation_busy 的演示路径） */}
          {showActiveBar && (
            <div className="mb-2 flex items-center justify-between rounded-lg bg-secondary px-3 py-1.5 text-xs text-secondary-foreground">
              <span className="flex items-center gap-2">
                <span
                  className="size-1.5 animate-pulse rounded-full bg-current"
                  aria-hidden
                />
                Agent 正在执行
                {active.session === sessionId ? "" : "（其他会话）"}…
              </span>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => active.runId && cancel.mutate(active.runId)}
                disabled={active.runId === "" || cancel.isPending}
              >
                <CircleStop aria-hidden />
                停止
              </Button>
            </div>
          )}

          {/* 输入区 */}
          <div className="flex items-end gap-2 border-t py-4">
            <Textarea
              ref={textareaRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  void send();
                }
              }}
              disabled={composerDisabled}
              placeholder={
                configured
                  ? "描述你的训练、提问，或发起变更…（Enter 发送，Shift+Enter 换行）"
                  : "未配置模型，请先到设置页完成配置"
              }
              className="flex-1 resize-none"
              rows={2}
            />
            <Button
              size="icon"
              aria-label="发送"
              onClick={() => void send()}
              disabled={composerDisabled}
            >
              <SendHorizonal />
            </Button>
          </div>
        </>
      )}
    </div>
  );
}
