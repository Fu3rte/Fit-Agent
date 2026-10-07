import type {
  HistoryEntryWire,
  HistoryRunWire,
  HistorySteeringWire,
  SessionHistoryWire,
} from "@/lib/contract";
import { preparedProfilePayload } from "@/lib/business";
import {
  PREPARE_PROFILE,
  type PendingOperation,
  type ReActEntry,
  type ReActRound,
} from "./reactAgent";

/** 分支内每个运行产出的节点集合：用于在多个运行共享请求节点时挑选实际产生节点者 */
function runsWithNodes(entries: HistoryEntryWire[]): Set<string> {
  const produced = new Set<string>();
  for (const entry of entries)
    if (entry.run_id !== null) produced.add(entry.run_id);
  return produced;
}

/**
 * 选择用户请求节点归属的运行（§3.3）：同一请求节点被多次执行时，优先实际产生节点者，
 * 其次仍是 running 者，最后取最早开始的运行。仅展示该用户节点一次。
 */
function pickRequestRun(
  runs: HistoryRunWire[],
  produced: Set<string>,
): HistoryRunWire | undefined {
  return (
    runs.find((run) => produced.has(run.run_id)) ??
    runs.find((run) => run.status === "running") ??
    [...runs].sort(
      (left, right) =>
        left.started_at - right.started_at ||
        left.run_id.localeCompare(right.run_id),
    )[0]
  );
}

/** 已提交节点 → 展示条目（§3.2、§3.4）：系统节点返回 null，消费输入只挂到关联用户节点；准备工具结果附带完整画像 */
function convertEntry(
  entry: HistoryEntryWire,
  consumedByEntry: Map<string, HistorySteeringWire>,
  toolArgs: Map<string, Record<string, unknown>>,
): ReActEntry | null {
  const { message } = entry;
  if (message.role === "system") return null;
  if (message.role === "user") {
    const consumed = consumedByEntry.get(entry.entry_id);
    return {
      kind: "user",
      id: entry.entry_id,
      entry_id: entry.entry_id,
      request: message.text,
      ...(consumed !== undefined
        ? {
            steering: {
              status: "consumed" as const,
              entry_id: entry.entry_id,
              reason: null,
            },
            steering_id: consumed.steering_id,
          }
        : {}),
    };
  }
  if (message.role === "assistant")
    return {
      kind: "assistant",
      id: entry.entry_id,
      content: message.content,
      stop_reason: message.stop_reason,
      entry_id: entry.entry_id,
      parent_id: entry.parent_id,
    };
  return {
    kind: "tool",
    id: message.tool_call_id,
    name: message.tool_name,
    arguments: toolArgs.get(message.tool_call_id) ?? {},
    content: message.content,
    status: message.is_error ? "failed" : "completed",
    entry_id: entry.entry_id,
    parent_id: entry.parent_id,
    ...(message.tool_name === PREPARE_PROFILE && !message.is_error
      ? { profile: preparedProfilePayload(message.content) }
      : {}),
  };
}

/**
 * 当前分支历史 → 展示轮次（§3）：按运行分组，祖先链顺序保持；系统节点不产生可见条目；
 * consumed Steering 只挂到关联用户节点一次；pending/withdrawn/discarded 输入按原运行展示。
 */
export function historyToRounds(history: SessionHistoryWire): ReActRound[] {
  const { entries, runs, steering } = history;
  const produced = runsWithNodes(entries);

  const consumedByEntry = new Map<string, HistorySteeringWire>();
  for (const input of steering)
    if (input.status === "consumed" && input.entry_id !== null)
      consumedByEntry.set(input.entry_id, input);

  const toolArgs = new Map<string, Record<string, unknown>>();
  for (const entry of entries)
    if (entry.message.role === "assistant")
      for (const block of entry.message.content)
        if (block.type === "tool_call")
          toolArgs.set(block.tool_call_id, block.arguments);

  const runById = new Map(runs.map((run) => [run.run_id, run]));
  const requestToRuns = new Map<string, HistoryRunWire[]>();
  for (const run of runs) {
    const list = requestToRuns.get(run.request_entry_id) ?? [];
    list.push(run);
    requestToRuns.set(run.request_entry_id, list);
  }

  const rounds: ReActRound[] = [];
  const roundByRun = new Map<string, ReActRound>();
  const ensureRound = (run: HistoryRunWire): ReActRound => {
    const existing = roundByRun.get(run.run_id);
    if (existing !== undefined) return existing;
    const round: ReActRound = {
      id: run.run_id,
      run_id: run.run_id,
      request_entry_id: run.request_entry_id,
      entries: [],
      status: run.status,
      ...(run.error_message !== null ? { error: run.error_message } : {}),
    };
    roundByRun.set(run.run_id, round);
    rounds.push(round);
    return round;
  };

  for (const entry of entries) {
    /* 用户节点优先归属以它为 request_entry_id 的运行（§3.3）：重新生成复用已消费 Steering
     * 节点时，该节点创建自旧运行但仍应展示在新运行下；无运行声明时按其创建运行归属。 */
    let owner: HistoryRunWire | undefined;
    if (entry.message.role === "user") {
      const claimed = requestToRuns.get(entry.entry_id) ?? [];
      owner =
        claimed.length > 0
          ? pickRequestRun(claimed, produced)
          : entry.run_id !== null
            ? runById.get(entry.run_id)
            : undefined;
    } else {
      owner = entry.run_id !== null ? runById.get(entry.run_id) : undefined;
    }
    if (owner === undefined) continue;
    const converted = convertEntry(entry, consumedByEntry, toolArgs);
    if (converted === null) continue;
    const round = ensureRound(owner);
    round.entries.push(converted);
  }

  /* 尚未产生节点的失败、取消、中断或执行中运行仍展示为空轮次（§3.3、§5.3） */
  for (const run of runs) if (!roundByRun.has(run.run_id)) ensureRound(run);

  /* pending/withdrawn/discarded 输入不伪造节点，按原运行展示（§3.4） */
  for (const input of steering) {
    if (input.status === "consumed") continue;
    const round = roundByRun.get(input.run_id);
    if (round === undefined) continue;
    round.entries.push({
      kind: "user",
      id: input.steering_id,
      steering_id: input.steering_id,
      request: input.text,
      steering:
        input.status === "pending"
          ? { status: "pending", entry_id: null, reason: null }
          : input.status === "withdrawn"
            ? { status: "withdrawn", entry_id: null, reason: null }
            : {
                status: "discarded",
                entry_id: null,
                reason: input.reason as never,
              },
    });
  }

  return rounds;
}

/** 未确认操作 → 占位轮次（§5.2、session-edit-regenerate-contract §8）：保留原正文并留下显式重试入口 */
export function operationRound(operation: PendingOperation): ReActRound {
  const base = {
    id: operation.operation_id,
    ...(operation.run_id !== null ? { run_id: operation.run_id } : {}),
    ...(operation.request_entry_id != null
      ? { request_entry_id: operation.request_entry_id }
      : {}),
  };
  if (operation.kind === "regenerate")
    return { ...base, entries: [], status: "unknown", pending: [operation] };
  const entry: ReActEntry =
    operation.kind === "steering"
      ? {
          kind: "user",
          id: operation.steering_id ?? operation.operation_id,
          steering_id: operation.steering_id ?? operation.operation_id,
          request: operation.request,
          steering: { status: "pending", entry_id: null, reason: null },
          operation_id: operation.operation_id,
        }
      : {
          kind: "user",
          id: operation.operation_id,
          request: operation.request,
        };
  return { ...base, entries: [entry], status: "unknown", pending: [operation] };
}

/**
 * 受理删除范围（session-position-contract §2、§3）：定位目标用户节点所在的展示轮次，
 * 保留其之前的条目，移除目标节点及其后的全部轮次与条目；返回被移除的目标节点供重新生成复用。
 * 目标按真实用户节点 ID 定位（entry_id），已消费 Steering 的展示 ID 与真实 ID 不同。
 */
export function pruneFrom(
  rounds: ReActRound[],
  targetEntryId: string,
): { kept: ReActRound[]; target?: ReActEntry } {
  const kept: ReActRound[] = [];
  for (const round of rounds) {
    const index = round.entries.findIndex(
      (entry) =>
        entry.kind === "user" && (entry.entry_id ?? entry.id) === targetEntryId,
    );
    if (index === -1) {
      kept.push(round);
      continue;
    }
    const remaining = round.entries.slice(0, index);
    if (remaining.length > 0) kept.push({ ...round, entries: remaining });
    return { kept, target: round.entries[index] };
  }
  return { kept, target: undefined };
}

/** 未确认操作需要继续处理的方式（§5.2）：运行查询收敛或按原 operation_id 查询受理结果 */
export interface LedgerRecovery {
  rounds: ReActRound[];
  /** 保持占用、由既有运行查询收敛的未确认运行 */
  watch: { roundId: string; runId: string }[];
  /** 按原 operation_id 查询受理结果的操作 */
  query: { roundId: string; operation: PendingOperation }[];
  /** 历史已完整覆盖、可移出账本的操作 */
  accepted: string[];
}

/**
 * 历史与本地未确认账本合并（§5.2）：历史为已提交事实，账本只保留历史未覆盖的未确认操作；
 * 已提交运行/输入按 run_id、steering_id、entry_id 去重，不通过文本判断重复。
 */
export function reconcileLedger(
  history: SessionHistoryWire | null,
  operations: PendingOperation[],
): LedgerRecovery {
  const rounds = history === null ? [] : historyToRounds(history);
  const roundByRun = new Map<string, ReActRound>();
  for (const round of rounds)
    if (round.run_id !== undefined) roundByRun.set(round.run_id, round);
  const steeringHistory = new Map<string, HistorySteeringWire>();
  if (history !== null)
    for (const input of history.steering)
      steeringHistory.set(input.steering_id, input);

  const watchByRun = new Map<string, { roundId: string; runId: string }>();
  const query: LedgerRecovery["query"] = [];
  const accepted: string[] = [];

  /* 历史中仍 running 的运行保持占用，由运行查询收敛（§5.3） */
  for (const round of rounds)
    if (round.status === "running" && round.run_id !== undefined)
      watchByRun.set(round.run_id, { roundId: round.id, runId: round.run_id });

  for (const operation of operations) {
    if (operation.kind !== "steering") {
      const committed =
        operation.run_id !== null
          ? roundByRun.get(operation.run_id)
          : undefined;
      if (committed !== undefined) {
        if (committed.status !== "running")
          accepted.push(operation.operation_id);
        continue;
      }
      const round = operationRound(operation);
      rounds.push(round);
      if (operation.run_id !== null) {
        roundByRun.set(operation.run_id, round);
        watchByRun.set(operation.run_id, {
          roundId: round.id,
          runId: operation.run_id,
        });
      } else {
        query.push({ roundId: round.id, operation });
      }
      continue;
    }
    const record =
      operation.steering_id != null
        ? steeringHistory.get(operation.steering_id)
        : undefined;
    if (record !== undefined) {
      if (record.status !== "pending") {
        accepted.push(operation.operation_id);
        continue;
      }
      const round = roundByRun.get(record.run_id);
      const entry = round?.entries.find(
        (item) => item.kind === "user" && item.id === record.steering_id,
      );
      if (entry !== undefined && entry.kind === "user")
        entry.operation_id = operation.operation_id;
      continue;
    }
    const runRound =
      operation.run_id !== null ? roundByRun.get(operation.run_id) : undefined;
    if (runRound !== undefined) {
      runRound.entries.push(operationRound(operation).entries[0]);
      query.push({ roundId: runRound.id, operation });
      continue;
    }
    const round = operationRound(operation);
    rounds.push(round);
    query.push({ roundId: round.id, operation });
  }

  return { rounds, watch: [...watchByRun.values()], query, accepted };
}

/**
 * 条目稳定身份（§5.2-4）：初始用户按 request_entry_id、Steering 按 steering_id（pending 快照
 * 与已消费节点同身份）、工具按 tool_call_id、助手按 entry_id；同身份只在展示中出现一次。
 */
function entryIdentity(round: ReActRound, entry: ReActEntry): string {
  if (entry.kind === "assistant")
    return `assistant:${entry.entry_id ?? entry.id}`;
  if (entry.kind === "tool") return `tool:${entry.id}`;
  if (entry.steering !== undefined)
    return `steering:${entry.steering_id ?? entry.id}`;
  return `user:${round.request_entry_id ?? entry.id}`;
}

/** 已确认终态（§5.2-7）：同身份归并时终态优先于旧快照 */
function settled(entry: ReActEntry): boolean {
  return (
    entry.kind === "user" &&
    entry.steering !== undefined &&
    entry.steering.status !== "pending"
  );
}

/**
 * 同一运行的条目合并（§5.2-6）：历史为已提交事实来源，按稳定身份补齐并替换同身份条目；
 * 同身份优先已确认终态；本地未提交快照按其原顺序插回对应位置，保持分支内顺序。
 */
function mergeEntries(
  existing: ReActRound,
  incoming: ReActRound,
): ReActEntry[] {
  const localByKey = new Map(
    existing.entries.map((entry) => [entryIdentity(existing, entry), entry]),
  );
  const indexOf = new Map<string, number>();
  const merged: ReActEntry[] = [];
  for (const entry of incoming.entries) {
    const key = entryIdentity(incoming, entry);
    const local = localByKey.get(key);
    indexOf.set(key, merged.length);
    merged.push(
      local !== undefined && settled(local) && !settled(entry) ? local : entry,
    );
  }
  let anchor: string | undefined;
  for (const entry of existing.entries) {
    const key = entryIdentity(existing, entry);
    if (indexOf.has(key)) {
      anchor = key;
      continue;
    }
    const at = anchor === undefined ? 0 : indexOf.get(anchor)! + 1;
    merged.splice(at, 0, entry);
    for (const [placed, index] of indexOf)
      if (index >= at) indexOf.set(placed, index + 1);
    indexOf.set(key, at);
    anchor = key;
  }
  return merged;
}

/**
 * 未确认操作仅保留历史未覆盖者（§5.2-4、§5.2-6）：历史已确认受理的操作移出展示的 pending，
 * 避免残留“提交结果未知 / 重试”；仍未知的操作与检索入口保留。
 */
function unconfirmedPending(
  pending: PendingOperation[] | undefined,
  confirmed: ReadonlySet<string>,
): PendingOperation[] | undefined {
  if (pending === undefined) return undefined;
  const remaining = pending.filter(
    (operation) => !confirmed.has(operation.operation_id),
  );
  return remaining.length === 0 ? undefined : remaining;
}

/**
 * 历史刷新并入既有展示（§5.2-6、§5.2-7、§5.3）：按 run_id 关联轮次、按稳定节点身份合并条目；
 * 历史终态优先于旧 running 快照，本页终态与仍在生成的未提交快照不被较早快照覆盖。
 */
export function mergeHistoryRounds(
  current: ReActRound[],
  history: ReActRound[],
  confirmed: ReadonlySet<string> = new Set(),
): ReActRound[] {
  const identity = (round: ReActRound): string => round.run_id ?? round.id;
  const local = new Map(current.map((round) => [identity(round), round]));
  const merged = history.map((round) => {
    const existing = local.get(identity(round));
    if (existing === undefined || existing.status === "unknown") return round;
    const entries = mergeEntries(existing, round);
    const base =
      existing.status === "running" &&
      round.status !== "running" &&
      round.status !== "unknown"
        ? round
        : existing;
    return {
      ...base,
      entries,
      pending: unconfirmedPending(existing.pending, confirmed),
    };
  });
  const covered = new Set(merged.map(identity));
  const extras = current.filter((round) => !covered.has(identity(round)));
  return extras.length === 0 ? merged : [...merged, ...extras];
}
