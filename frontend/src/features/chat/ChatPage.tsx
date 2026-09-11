/**
 * 对话页（/）：居中消息流 + 草稿卡闭环。
 * 数据全部经 src/lib/api.ts 走 mock 中间件（真实 fetch + 原生 EventSource，
 * 客户端代码为将来直连后端的形状）。SSE 事件见 src/lib/contract.ts 契约 v1。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  AlertCircle,
  CircleStop,
  Loader2,
  RefreshCw,
  SendHorizontal,
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
  discardDraft,
  getActiveRun,
  getMessages,
  getProfile,
  getProvider,
  getSessionDrafts,
  getSessions,
  recalcDraft,
  reviseDraft,
} from "@/lib/api";
import type {
  ActiveRunInfo,
  ChatMessage,
  Draft,
  DraftPayload,
  ErrorCode,
  FieldDiff,
  RunStatus,
  SseEvent,
} from "@/lib/contract";
import { toApiError } from "./apiError";
import { DraftCard } from "./DraftCard";
import { Markdown } from "./Markdown";
import {
  RECOVERY_HINT,
  RECOVERY_UNKNOWN_CAUSE,
  RUN_STATUS_COPY,
  failureReasonCopy,
  runPhaseCopy,
} from "./runCopy";
import { useChatEvents, type ConnectionLostReason } from "./useChatEvents";

/** Run 完成后需失效的数据类 query（records/profile/stats/review 由各看板页消费） */
const DATA_QUERY_KEYS = ["records", "stats", "profile", "review"] as const;

interface ActiveRun {
  runId: string;
  /** 发起 run 的会话；流式气泡只在该会话内展示 */
  session: string;
  userText: string;
  text: string;
  drafts: Draft[];
  compacted: boolean;
  /** run.started 已收到：区分 8.8「处理中」的次要文案（受理中 / 执行中），不新造状态 */
  started: boolean;
  /** context.compacting 至 context.compacted 之间的常驻指示（08 8.8 压缩两态） */
  compacting: boolean;
  /**
   * 查询恢复期（08 8.7 规则 1/3）：SSE 不可用，本 Run 的文本/草稿/状态一律以
   * GET /api/runs/active 的查询结果为准（替换本地展示，不拼接），本次不接回 SSE。
   */
  recovering: boolean;
}

/**
 * 已终结但仍需在会话内保留的 Run（08 8.8：取消/失败保留中断前文本并标明「输出未完成」，
 * 关联草稿一并保留；不自动恢复执行，重试由用户显式发起新的 Run）。
 * completed 不入此列：完整回答由服务端落库后经消息列表呈现，避免重复拼接。
 */
interface ClosedRun {
  key: string;
  session: string;
  status: Extract<RunStatus, "cancelled" | "failed">;
  userText: string;
  /** 中断前已流出的文本（mock 阶段等同「已保存部分」） */
  text: string;
  drafts: Draft[];
  /** failed 的可理解原因文案 */
  reason?: string;
}

type Item =
  | { kind: "message"; message: ChatMessage }
  | { kind: "user"; text: string }
  | { kind: "compacted" }
  | {
      kind: "stream";
      text: string;
      drafts: Draft[];
      compacting: boolean;
      /** 查询恢复期：常驻「连接中断，正在恢复」，不报错、不判失败（08 8.7 规则 1） */
      recovering: boolean;
    }
  | { kind: "closed"; run: ClosedRun };

/**
 * SSE 订阅桥（F0-04B）：恢复周期结束后由父级换 key 重建订阅，取回一个全新连接。
 * useChatEvents 的订阅 effect 只在挂载时建立，闭合并行重连入口，故用挂载/卸载表达
 * 「一次新的订阅」——不改 useChatEvents 本身。
 */
function ChatEventsBridge({
  onEvent,
  onConnectionLost,
}: {
  onEvent: (event: SseEvent) => void;
  onConnectionLost: (reason: ConnectionLostReason) => void;
}) {
  useChatEvents(onEvent, { onConnectionLost });
  return null;
}

/** 压缩快速完成判定窗口（08 8.8 验收 10：3 秒内完成 → 轻提示 3 秒消失） */
const FAST_COMPACTION_MS = 3000;

/** 查询恢复的正常轮询间隔（08 8.7 规则 6：查询成功即恢复到此间隔） */
const RECOVERY_POLL_MS = 1_000;

/**
 * 查询退避档位（08 8.7 规则 6，对齐 stage0 字面 1/2/4/8/15s）：以「连续失败次数」取档，
 * 0 次失败 = 正常间隔 1s；第 1 次失败起依次 1/2/4/8/15s，上限 15s；不新增配置系统（规则 3）。
 */
const RECOVERY_BACKOFF_MS = [1_000, 2_000, 4_000, 8_000, 15_000] as const;

const recoveryBackoffMs = (failures: number): number =>
  RECOVERY_BACKOFF_MS[
    Math.min(Math.max(failures, 0), RECOVERY_BACKOFF_MS.length - 1)
  ];

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

  /** 未建档（契约 profile = null）：对话页只给一句引导，不改成表单、不禁用输入 */
  const profile = useQuery({ queryKey: ["profile"], queryFn: getProfile });
  const uncreatedProfile = profile.data?.profile === null;

  const messages = useQuery({
    queryKey: ["messages", sessionId],
    queryFn: () => getMessages(sessionId as string),
    enabled: sessionId !== null,
  });

  /* -------------------------------- 本地态 -------------------------------- */
  const [input, setInput] = useState("");
  const [active, setActive] = useState<ActiveRun | null>(null);
  /** 取消/失败后保留下来的 Run（文本 + 草稿 + 终态标记） */
  const [closed, setClosed] = useState<ClosedRun[]>([]);
  /** 最近一次 completed 的整段文本：落到消息列表后在尾部标「已完成」 */
  const [completedText, setCompletedText] = useState<string | null>(null);
  /** 压缩开始时刻（仅供快/慢判定与一次性 toast，不参与渲染） */
  const compactingAtRef = useRef<number | null>(null);
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
  /** SSE 订阅代次：恢复周期结束后 +1 重建订阅（见 ChatEventsBridge） */
  const [subscribeCycle, setSubscribeCycle] = useState(0);

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

  /* --------------------------- 查询恢复：状态位与小工具 --------------------------- */
  /**
   * 恢复闩（08 8.7 规则 7）：置位期间只允许一个轮询循环——visibilitychange 反复触发、
   * onConnectionLost 连发、刷新后已连接又失效等情形均不叠加第二个循环。
   * 只在终态 / 无从查询 / 页面卸载时落下（卸载后重新订阅即为新的页面周期）。
   */
  const recoveringRef = useRef(false);
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  /** 连续查询失败次数 → 退避档位；查询成功即归零（规则 6） */
  const pollFailuresRef = useRef(0);
  /** 本次恢复所跟踪的 Run；null = 尚未确认（乐观窗口无 run_id） */
  const trackedRunIdRef = useRef<string | null>(null);
  const mountedRef = useRef(true);
  /** 本恢复周期内连接确实被断开过（决定终态后是否换新订阅） */
  const connectionDroppedRef = useRef(false);
  /** 挂载查询只做一次（StrictMode 重挂载不重复查询） */
  const mountCheckedRef = useRef(false);
  /** 轮询实现的自引用：计时器回调总取最新实现，避免 useCallback 循环依赖 */
  const pollRef = useRef<() => Promise<void>>(async () => {});

  /** 清理轮询计时器（规则 7：终态或页面卸载必须清计时器） */
  const clearPollTimer = useCallback(() => {
    if (pollTimerRef.current === null) return;
    clearTimeout(pollTimerRef.current);
    pollTimerRef.current = null;
  }, []);

  /** 草稿按 draft.id 归并（规则 2：当前状态以业务接口查询为准），同 id 覆盖不产生重复卡 */
  const mergeDrafts = useCallback((incoming: Draft[]) => {
    if (incoming.length === 0) return;
    setDrafts((prev) => {
      const next = { ...prev };
      for (const draft of incoming) next[draft.id] = draft;
      return next;
    });
  }, []);

  /** 恢复态下用户原文无从取得（查询不返回用户消息）：取该会话已加载消息的最后一条 user */
  const lastUserTextOf = useCallback(
    (runSession: string): string => {
      const loaded =
        queryClient.getQueryData<ChatMessage[]>(["messages", runSession]) ?? [];
      for (const message of [...loaded].reverse())
        if (message.role === "user") return message.content;
      return "";
    },
    [queryClient],
  );

  /** 草稿卡按会话草稿查询恢复当前状态（08 8.7 规则 2：通知不是事实来源） */
  const restoreSessionDrafts = useCallback(
    (runSession: string) => {
      void getSessionDrafts(runSession).then(mergeDrafts, () => {
        // 会话草稿查询失败不阻断恢复：轮询载荷中的 run.drafts 仍会刷新卡片状态
      });
    },
    [mergeDrafts],
  );

  /* ----------------------------- 终态收尾（保留已流出内容） ----------------------------- */
  /** 保留项入列（08 8.8：中断前文本 + 草稿 + 终态标记）并刷新数据类 query */
  const pushClosedRun = useCallback(
    (run: Omit<ClosedRun, "key">) => {
      setClosed((prev) => [...prev, { key: crypto.randomUUID(), ...run }]);
      invalidateData();
    },
    [invalidateData],
  );

  /**
   * 取消/失败：不丢弃已流出文本与关联草稿，转为会话内保留项（08 8.8）。
   * 执行不自动恢复；重试仅由用户显式发起（见 closed 气泡中的重试按钮）。
   */
  const closeActive = useCallback(
    (status: ClosedRun["status"], errorCode?: ErrorCode, cause?: string) => {
      const cur = activeRef.current;
      if (!cur) return;
      pushClosedRun({
        session: cur.session,
        status,
        // 恢复来的 Run 无本地原文（查询不返回用户消息）：按已加载消息回填，取不到为空
        userText: cur.userText || lastUserTextOf(cur.session),
        text: cur.text,
        drafts: cur.drafts,
        reason:
          cause ??
          (status === "failed"
            ? failureReasonCopy(errorCode ?? "invalid_request")
            : undefined),
      });
      compactingAtRef.current = null;
      setActive(null);
      activeRef.current = null;
      // 终态已被取消/失败取代：撤下上一条「已完成」标记
      setCompletedText(null);
    },
    [lastUserTextOf, pushClosedRun],
  );

  /* --------------------------- 查询恢复：轮询循环（08 8.7） --------------------------- */
  /**
   * 结束恢复周期（规则 7）：落闩、清计时器；本次连接确实被断开过时换新订阅，
   * 使后续新 Run 仍有事件可用（本周期内始终不接回 SSE——规则 3）。
   */
  const endRecovery = useCallback(() => {
    recoveringRef.current = false;
    trackedRunIdRef.current = null;
    pollFailuresRef.current = 0;
    clearPollTimer();
    if (!connectionDroppedRef.current) return;
    connectionDroppedRef.current = false;
    setSubscribeCycle((n) => n + 1);
  }, [clearPollTimer]);

  /**
   * 把 GET /api/runs/active 的结果落到本地展示（规则 2/3：以查询结果替换本地展示，
   * 不拼接、不重放通知）。返回 true = 该 Run 仍在执行，需继续轮询至终态。
   */
  const applyActiveRunSnapshot = useCallback(
    (info: ActiveRunInfo | null): boolean => {
      const tracked = trackedRunIdRef.current;
      // 服务端已无可查询 Run：无从确认终态，停止等待，不自动恢复执行
      if (info === null) {
        const cur = activeRef.current;
        if (cur !== null && (cur.runId === "" || cur.runId === tracked))
          closeActive("failed", undefined, RECOVERY_UNKNOWN_CAUSE);
        return false;
      }
      // 查询到的是另一个 Run（本地已发起新 Run）：不覆盖本地展示
      if (tracked !== null && tracked !== "" && tracked !== info.run_id)
        return false;
      trackedRunIdRef.current = info.run_id;
      // 规则 2：草稿当前状态以业务接口查询为准，按 id 归并不产生重复卡
      mergeDrafts(info.drafts);
      restoreSessionDrafts(info.session_id);

      switch (info.status) {
        case "pending":
        case "running": {
          const cur = activeRef.current;
          const next: ActiveRun = {
            runId: info.run_id,
            session: info.session_id,
            userText: cur?.userText || lastUserTextOf(info.session_id),
            // 规则 2/3：文本一律以已保存部分替换，不拼接
            text: info.saved_text,
            drafts: info.drafts,
            compacted: cur?.compacted ?? false,
            started: info.status === "running",
            compacting: cur?.compacting ?? false,
            recovering: true,
          };
          activeRef.current = next;
          setActive(next);
          return true;
        }
        case "completed": {
          // 完整回答由服务端落库后经消息列表呈现，不重复拼接（08 8.8）
          activeRef.current = null;
          setActive(null);
          compactingAtRef.current = null;
          setCompletedText(info.saved_text);
          invalidateData();
          return false;
        }
        default: {
          // cancelled / failed：保留已保存部分与草稿，标明输出未完成（08 8.8）
          const cur = activeRef.current;
          if (cur === null) {
            // 刷新后直接落在终态（含服务重启遗留 failed）：按保留项渲染
            pushClosedRun({
              session: info.session_id,
              status: info.status,
              userText: lastUserTextOf(info.session_id),
              text: info.saved_text,
              drafts: info.drafts,
              reason:
                info.status === "failed"
                  ? failureReasonCopy(info.error_code ?? "invalid_request")
                  : undefined,
            });
          } else {
            activeRef.current = {
              ...cur,
              runId: info.run_id,
              session: info.session_id,
              text: info.saved_text,
              drafts: info.drafts,
            };
            closeActive(info.status, info.error_code);
          }
          return false;
        }
      }
    },
    [
      closeActive,
      invalidateData,
      lastUserTextOf,
      mergeDrafts,
      pushClosedRun,
      restoreSessionDrafts,
    ],
  );

  /** 轮询一次并按退避安排下一次（规则 3/6）：成功即回到正常间隔，失败按 1/2/4/8/15s */
  const pollActiveRun = useCallback(async (): Promise<void> => {
    if (!mountedRef.current || !recoveringRef.current) return;
    let keepPolling: boolean;
    try {
      const res = await getActiveRun();
      pollFailuresRef.current = 0;
      keepPolling = applyActiveRunSnapshot(res.run);
    } catch {
      pollFailuresRef.current += 1;
      keepPolling = true;
    }
    if (!mountedRef.current || !recoveringRef.current) return;
    if (!keepPolling) {
      endRecovery();
      return;
    }
    const failures = pollFailuresRef.current;
    // 第 1 次失败 → 1s，之后 2/4/8/15s（字面对齐 08 8.7 规则 6）；0 次失败用正常间隔
    const interval =
      failures === 0 ? RECOVERY_POLL_MS : recoveryBackoffMs(failures - 1);
    clearPollTimer();
    pollTimerRef.current = setTimeout(() => {
      pollTimerRef.current = null;
      void pollRef.current();
    }, interval);
  }, [applyActiveRunSnapshot, clearPollTimer, endRecovery]);

  useEffect(() => {
    pollRef.current = pollActiveRun;
  }, [pollActiveRun]);

  /**
   * 进入恢复（规则 1/3/5/7）：置闩后立即查询一次，再按退避轮询至终态。
   * 闩在终态或新订阅建立前一直置位——连发的断线通知、可见性反复切换都不叠加第二个循环。
   */
  const beginRecovery = useCallback(() => {
    if (!mountedRef.current || recoveringRef.current) return;
    const cur = activeRef.current;
    if (cur === null) return;
    recoveringRef.current = true;
    trackedRunIdRef.current = cur.runId === "" ? null : cur.runId;
    pollFailuresRef.current = 0;
    // 规则 1：只提示，不取消、不重跑、不判失败
    const next: ActiveRun = { ...cur, recovering: true };
    activeRef.current = next;
    setActive(next);
    restoreSessionDrafts(cur.session);
    void pollActiveRun();
  }, [pollActiveRun, restoreSessionDrafts]);

  /** 连接不可用（08 8.7 规则 5）：有进行中 Run 则转查询恢复，否则取回新订阅 */
  const handleConnectionLost = useCallback(
    (_reason: ConnectionLostReason) => {
      if (activeRef.current !== null) {
        connectionDroppedRef.current = true;
        beginRecovery();
        return;
      }
      // 无进行中 Run：无内容可恢复、不提示；先探一次业务接口确认服务可达再重建订阅，
      // 避免服务不可用时形成重连风暴（规则 5：不依赖自动重连）
      void getActiveRun().then(
        () => setSubscribeCycle((n) => n + 1),
        () => {
          // 服务仍不可达：不重连、不轮询；等下一次页面级复查
        },
      );
    },
    [beginRecovery],
  );

  /** 挂载即查询一次（规则 2/7）：活跃 Run 转恢复轮询，终态者按终态渲染 */
  useEffect(() => {
    mountedRef.current = true;
    if (mountCheckedRef.current) return;
    mountCheckedRef.current = true;
    void (async () => {
      try {
        const res = await getActiveRun();
        if (!mountedRef.current) return;
        if (!applyActiveRunSnapshot(res.run)) return;
        // 规则 3：本次不接回 SSE，只轮询至终态
        recoveringRef.current = true;
        void pollActiveRun();
      } catch {
        // 查询失败：无事实可依，不改本地展示；断线/可见性复查时会再试
      }
    })();
  }, [applyActiveRunSnapshot, pollActiveRun]);

  /** 规则 7：页面恢复可见时立即复查一次，清掉待执行计时器后立刻查询（不叠加循环） */
  useEffect(() => {
    const onVisibility = () => {
      if (document.visibilityState !== "visible") return;
      if (!mountedRef.current || !recoveringRef.current) return;
      clearPollTimer();
      void pollActiveRun();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, [clearPollTimer, pollActiveRun]);

  /** 卸载（规则 7）：落闩并清计时器，不再发起查询 */
  useEffect(
    () => () => {
      mountedRef.current = false;
      recoveringRef.current = false;
      clearPollTimer();
    },
    [clearPollTimer],
  );

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
            // runId 有两个来源：POST /api/runs 响应（pending 期即可取消）与本事件；
            // 空 runId（乐观窗口）与已持有同一 runId 者均据事件确认转 running。
            prev && (prev.runId === "" || prev.runId === ev.run_id)
              ? { ...prev, runId: ev.run_id, started: true }
              : prev,
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
        case "context.compacting":
          // 压缩两态之一：常驻「正在整理上下文」，直到 context.compacted 撤下。
          // 压缩失败由后端自动恢复，展示层不报错、不改 Run 状态。
          if (mine(ev.run_id)) {
            compactingAtRef.current = Date.now();
            setActive((prev) =>
              prev && mine(ev.run_id) ? { ...prev, compacting: true } : prev,
            );
          }
          break;
        case "context.compacted": {
          // B3：上下文压缩仅界面轻提示，不做轨迹面板
          if (!mine(ev.run_id)) break;
          const since = compactingAtRef.current;
          compactingAtRef.current = null;
          if (since !== null && Date.now() - since <= FAST_COMPACTION_MS) {
            // 快速完成：轻提示 3 秒后消失，不留常驻指示
            toast("已整理上下文", {
              description: ev.note,
              duration: 3000,
            });
          }
          setActive((prev) =>
            prev && mine(ev.run_id)
              ? { ...prev, compacting: false, compacted: true }
              : prev,
          );
          break;
        }
        case "run.completed":
          if (mine(ev.run_id)) {
            const finished = activeRef.current;
            if (finished) setCompletedText(finished.text);
            setActive(null);
            activeRef.current = null;
            invalidateData();
          }
          break;
        case "run.cancelled":
          if (mine(ev.run_id)) closeActive("cancelled");
          break;
        case "run.failed":
          if (mine(ev.run_id)) closeActive("failed", ev.error_code);
          break;
      }
    },
    [closeActive, invalidateData],
  );

  /* -------------------------------- 发送 ---------------------------------- */
  /**
   * 发起一个新 Run：每次均使用新的 client_request_id（幂等键），
   * 因此终态后的手动重试就是新建 Run，不恢复、不重放原有执行（08 8.4/8.8）。
   */
  const submit = async (text: string, restore?: (text: string) => void) => {
    if (!text || !sessionId || !configured) return;
    // 运行中不做乐观覆盖：覆盖会以 runId:"" 顶掉原 run，致其 delta 串扰进新流或被丢弃。
    // 空闲时才挂乐观 active；运行中再发送由 409 conversation_busy 走全局 toast（演示路径）。
    const wasIdle = activeRef.current === null;
    if (wasIdle) {
      setCompletedText(null);
      compactingAtRef.current = null;
      setActive({
        runId: "",
        session: sessionId,
        userText: text,
        text: "",
        drafts: [],
        compacted: false,
        started: false,
        compacting: false,
        recovering: false,
      });
    }
    try {
      const handle = await createRun({
        session_id: sessionId,
        message: text,
        client_request_id: crypto.randomUUID(),
      });
      // 08 8.1/8.3：pending 与 running 均可取消。POST /api/runs 一返回即写回 run_id，
      // 使「停止」在 run.started 之前对 pending Run 可用；runId==="" 的乐观窗口逻辑不变。
      if (handle.run_id) {
        // 同步补写 ref：SSE 归属判定（mine）读 activeRef.current，不能等 effect 刷新
        const cur = activeRef.current;
        if (
          cur !== null &&
          cur.session === sessionId &&
          cur.runId === "" // 已由 run.started 赋过 id 则不覆写
        ) {
          activeRef.current = { ...cur, runId: handle.run_id };
        }
        setActive((prev) =>
          prev && prev.session === sessionId && prev.runId === ""
            ? { ...prev, runId: handle.run_id }
            : prev,
        );
      }
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
      if (wasIdle) restore?.(text); // 保留输入便于重试；busy 时未清空，无需恢复
    }
  };

  const send = () => {
    const text = input.trim();
    if (!text) return;
    // 空闲才清空输入；运行中 busy 路径保留输入（A5 演示）
    if (activeRef.current === null) setInput("");
    void submit(text, setInput);
  };

  /** 终态后的手动重试：以原文本重新发送 = 新 Run + 新 client_request_id */
  const retry = (run: ClosedRun) => {
    void submit(run.userText);
  };

  const cancel = useMutation({
    mutationFn: (runId: string) => cancelRun(runId),
    onSuccess: (res) => {
      if (res.status === "cancelled") toast.success("已取消当前任务");
    },
    onError: (error) => toast.error(toApiError(error).message),
  });

  /* ------------------------------ 草稿操作 -------------------------------- */
  /**
   * 409 draft_modified（01 1.4：所见修订版本与服务端不一致，拒绝确认已被修改的草稿）：
   * 经会话草稿查询取回服务端真相，按 draft.id 归并覆盖本地卡（payload/diff/revision）
   * 重渲染（01 1.2：当前状态走业务接口，不依赖历史通知）。返回是否取到该草稿。
   */
  const refreshDraftFromServer = useCallback(
    async (draftId: string): Promise<boolean> => {
      const targets = [
        ...new Set(
          [sessionIdRef.current, activeRef.current?.session].filter(
            (s): s is string => s !== null && s !== undefined,
          ),
        ),
      ];
      for (const session of targets) {
        try {
          const list = await getSessionDrafts(session);
          mergeDrafts(list);
          if (list.some((d) => d.id === draftId)) return true;
        } catch {
          // 查询失败：不改本地展示，仅提示用户；下一次操作可再试
        }
      }
      return false;
    },
    [mergeDrafts],
  );

  /** 撤下本地纠错缓存：服务端真相已就位，卡片改由服务端 payload/diff 驱动 */
  const clearEdit = (draftId: string) =>
    setEdits((prev) => {
      if (!(draftId in prev)) return prev;
      const next = { ...prev };
      delete next[draftId];
      return next;
    });

  /** 修订冲突提示：拉取最新草稿并让卡片按服务端真相重渲染（01 1.4） */
  const onDraftModified = (draftId: string) => {
    toast.error("草稿已被修改", {
      description: "正在拉取最新草稿，请核对后再次确认。",
    });
    void refreshDraftFromServer(draftId).then((found) => {
      if (found) clearEdit(draftId);
    });
  };

  const confirm = useMutation({
    mutationFn: (draft: Draft) => confirmDraft(draft.id, draft.revision),
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
      } else if (err.error_code === "draft_modified") {
        onDraftModified(draft.id);
      } else {
        toast.error(err.message ?? "确认失败");
      }
    },
  });

  /**
   * 内联纠错（01 1.2/1.3）：编辑提交经纠错业务接口，服务端整份替换 payload 并 revision+1；
   * 用返回的草稿替换本地卡（payload + diff + revision），不自动提交。
   */
  const revise = useMutation({
    mutationFn: (vars: { draftId: string; payload: DraftPayload }) =>
      reviseDraft(vars.draftId, vars.payload),
    onSuccess: (result) => {
      setDrafts((prev) => ({ ...prev, [result.draft.id]: result.draft }));
      clearEdit(result.draft.id);
      toast.success("草稿已更新", { description: "请确认后生效。" });
    },
    onError: (error, vars) => {
      const err = toApiError(error);
      if (err.error_code === "draft_modified") onDraftModified(vars.draftId);
      else toast.error(err.message ?? "纠错失败");
    },
  });

  /** 丢弃待确认草稿（01 1.3）：正式数据与业务版本不变；已丢弃不可确认 */
  const discard = useMutation({
    mutationFn: (draftId: string) => discardDraft(draftId),
    onSuccess: (result) => {
      setDrafts((prev) => {
        const draft = prev[result.draft_id];
        if (!draft) return prev;
        return {
          ...prev,
          [result.draft_id]: { ...draft, status: result.status },
        };
      });
      clearEdit(result.draft_id);
      toast.success("已丢弃草稿");
    },
    onError: (error) => toast.error(toApiError(error).message),
  });

  /** 已丢弃草稿不得再提交（01 1.3）：确认入口在页内统一拦截 */
  const requestConfirm = (draft: Draft) => {
    if (draft.status === "discarded") {
      toast.error("草稿已丢弃，不可确认");
      return;
    }
    confirm.mutate(draft);
  };

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
        onConfirm={() => requestConfirm(draft)}
        onRecalc={() => recalc.mutate(draft.id)}
        onRevise={(payload) => revise.mutate({ draftId: draft.id, payload })}
        onDiscard={() => discard.mutate(draft.id)}
        confirmPending={confirm.isPending && confirm.variables?.id === draft.id}
        recalcPending={recalc.isPending && recalc.variables === resolvedId}
        revisePending={
          revise.isPending && revise.variables?.draftId === draft.id
        }
        discardPending={discard.isPending && discard.variables === draft.id}
        staleError={staleIds.has(draft.id)}
        recalcDiff={recalcDiffs[draft.id]}
        superseded={
          replacedBy[draft.id] !== undefined || draft.status === "stale"
        }
      />
    );
  };

  const loaded = messages.data ?? [];
  const lastMessageId = loaded.at(-1)?.id;
  const closedHere = closed.filter((run) => run.session === sessionId);
  const items: Item[] = [];
  // 保留项按发起它的用户消息后就地插入（该用户消息已在服务端落库，不重复渲染）；
  // 列表刷新前暂存尾部，刷新后自然锚定到位，不会出现两份同文本气泡。
  const anchored = new Set<string>();
  for (const message of loaded) {
    items.push({ kind: "message", message });
    if (message.role !== "user") continue;
    const hit = closedHere.find(
      (run) => !anchored.has(run.key) && run.userText === message.content,
    );
    if (hit) {
      anchored.add(hit.key);
      items.push({ kind: "closed", run: hit });
    }
  }
  for (const run of closedHere)
    if (!anchored.has(run.key)) items.push({ kind: "closed", run });
  if (active && active.session === sessionId) {
    // 恢复期用户原文已在消息列表落库（规则 2：不重复拼接），不再渲染本地原文气泡
    if (!active.recovering) items.push({ kind: "user", text: active.userText });
    if (active.compacted) items.push({ kind: "compacted" });
    items.push({
      kind: "stream",
      text: active.text,
      drafts: active.drafts,
      compacting: active.compacting,
      recovering: active.recovering,
    });
  }

  const scrollRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [
    messages.data,
    active?.text,
    active?.drafts.length,
    active?.compacted,
    active?.compacting,
    closed.length,
  ]);

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
    <div className="relative mx-auto flex h-full w-full max-w-3xl flex-col px-6">
      <ChatEventsBridge
        key={subscribeCycle}
        onEvent={handleEvent}
        onConnectionLost={handleConnectionLost}
      />
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
            className="flex-1 space-y-4 overflow-y-auto pb-40"
          >
            {/* 未建档引导：建档只能由对话发起（PRD §5.2），只是文案，不影响发送 */}
            {uncreatedProfile && (
              <p className="rounded-lg border bg-card px-3 py-2 text-xs text-muted-foreground">
                尚未建档：建档只能通过对话完成（无独立表单）。按提示提供目标、经验、频率、时长、器械、体重、动作限制与身体情况，即可生成档案草稿。
              </p>
            )}
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
                case "message": {
                  const showCompleted =
                    item.message.role === "assistant" &&
                    item.message.id === lastMessageId &&
                    completedText !== null &&
                    item.message.content.trim() === completedText.trim();
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
                        {/* 08 8.8：completed → 已完成（Run 完成不等于草稿已确认） */}
                        {showCompleted ? (
                          <p className="mt-2 border-t pt-2 text-[11px] text-muted-foreground">
                            {RUN_STATUS_COPY.completed}
                            {item.message.draft_id
                              ? "·草稿待确认，尚未生效"
                              : ""}
                          </p>
                        ) : null}
                      </div>
                    </div>
                  );
                }
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
                        ) : item.recovering ? null : (
                          <p className="animate-pulse text-xs text-muted-foreground">
                            正在思考…
                          </p>
                        )}
                        {/* 08 8.7 规则 1：恢复期常驻提示，不报错、不判失败 */}
                        {item.recovering ? (
                          <p className="mt-2 flex items-center gap-1.5 text-xs text-muted-foreground">
                            <Loader2
                              className="size-3 animate-spin"
                              aria-hidden
                            />
                            {RECOVERY_HINT}
                          </p>
                        ) : null}
                        {/* 08 8.8 压缩两态：常驻指示，直到 context.compacted 撤下 */}
                        {item.compacting ? (
                          <p className="mt-2 flex items-center gap-1.5 text-xs text-muted-foreground">
                            <Loader2
                              className="size-3 animate-spin"
                              aria-hidden
                            />
                            正在整理上下文…
                          </p>
                        ) : null}
                        {item.drafts.map((d) => renderDraftCard(d))}
                      </div>
                    </div>
                  );
                case "closed": {
                  const run = item.run;
                  return (
                    <div key={run.key} className="flex justify-start">
                      <div className="w-full max-w-[85%] rounded-xl border bg-bubble-in px-4 py-3 text-sm text-bubble-in-foreground">
                        {run.text ? (
                          <Markdown text={run.text} />
                        ) : (
                          <p className="text-xs text-muted-foreground">
                            中断前尚无输出内容。
                          </p>
                        )}
                        {run.drafts.map((d) => renderDraftCard(d))}
                        {/* 终态标记：保留文本必须标明输出未完成（08 8.8） */}
                        <div className="mt-3 flex flex-wrap items-center gap-x-2 gap-y-1 border-t pt-2 text-xs">
                          <span className="flex items-center gap-1 rounded-full bg-secondary px-2 py-0.5 text-[11px] text-secondary-foreground">
                            输出未完成
                          </span>
                          <span className="flex items-center gap-1 font-medium">
                            <AlertCircle className="size-3.5" aria-hidden />
                            {RUN_STATUS_COPY[run.status]}
                          </span>
                          {run.reason ? (
                            <span className="text-muted-foreground">
                              {run.reason}
                            </span>
                          ) : null}
                          <span className="ml-auto flex items-center gap-2">
                            <span className="text-[11px] text-muted-foreground">
                              不会自动恢复执行
                            </span>
                            <Button
                              size="sm"
                              variant="outline"
                              onClick={() => retry(run)}
                              disabled={composerDisabled}
                            >
                              <RefreshCw aria-hidden />
                              重试
                            </Button>
                          </span>
                        </div>
                      </div>
                    </div>
                  );
                }
              }
            })}
          </div>

          {/* 输入容器：贴底浮层 + 上方渐变蒙板，消息从下方滚过 */}
          <div className="absolute inset-x-0 bottom-0 z-10 bg-background px-6 pt-6 pb-8">
            {/* 蒙板：贴着容器上沿，从背景色向透明淡出 */}
            <div
              aria-hidden
              className="pointer-events-none absolute inset-x-0 bottom-full h-10 bg-linear-to-t from-background to-transparent"
            />

            {/* 运行中状态条 + 停止（输入不禁用：conversation_busy 的演示路径） */}
            {showActiveBar && (
              <div className="mb-2 flex items-center justify-between rounded-lg bg-secondary px-3 py-1.5 text-xs text-secondary-foreground">
                <span className="flex items-center gap-2">
                  <span
                    className="size-1.5 animate-pulse rounded-full bg-current"
                    aria-hidden
                  />
                  {/* 08 8.8：pending/running 统一「处理中」，次要文案区分受理中/执行中 */}
                  {RUN_STATUS_COPY[active.started ? "running" : "pending"]}
                  <span className="text-muted-foreground">
                    {runPhaseCopy(active.started)}
                    {active.session === sessionId ? "" : "·其他会话"}
                  </span>
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

            {/* 输入区：胶囊形输入框，发送按钮内嵌，仅有内容时显示 */}
            <div className="relative">
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
                className="min-h-0 resize-none rounded-full border-0 bg-card py-4 pr-14 pl-5 shadow-md focus-visible:ring-0"
                rows={1}
              />
              {input.trim() !== "" && (
                <Button
                  size="icon"
                  aria-label="发送"
                  onClick={() => void send()}
                  disabled={composerDisabled}
                  className="absolute top-1/2 right-5 size-8 -translate-y-1/2 cursor-pointer"
                >
                  <SendHorizontal />
                </Button>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
