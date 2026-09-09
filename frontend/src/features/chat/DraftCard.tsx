/**
 * 草稿卡（PLAN-FRONTEND「草稿卡必含元素」）：
 * - 结构化字段级 Diff（A4）：旧值→新值，无旧值标「新增」；
 * - 关键字段内联纠错：只改待确认草稿（受控 payload），Diff 与展示实时更新，不自动提交；
 *   未提交的纠错经「提交纠错」走纠错业务接口（01 1.2），确认写入的是服务端最新草稿；
 * - 确认采纳幂等；409 draft_stale → 错误态 + 按最新数据一键重算；
 * - 重算产生的新草稿展示新旧草稿 Diff，再次确认才生效；
 * - 丢弃待确认草稿（01 1.3）：已丢弃徽章 + 只读，不得再提交。
 */
import { CircleAlert, RefreshCcw, Sparkles } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type {
  Draft,
  DraftKind,
  DraftPayload,
  FieldDiff,
  RecordDraftPayload,
  RecordSet,
} from "@/lib/contract";

const KIND_LABEL: Record<DraftKind, string> = {
  training_record: "训练记录草稿",
  plan_adjust: "计划调整草稿",
  profile_update: "档案变更草稿",
};

const fmt = (v: number | string | undefined, fallback = "—") =>
  v === undefined || v === "" ? fallback : String(v);

/* ------------------------------- Diff 列表 -------------------------------- */

/** 单行字段 Diff（旧值→新值，无旧值标「新增」） */
function FieldRow({ row }: { row: FieldDiff }) {
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs">
      <span className="min-w-32 text-muted-foreground">{row.field}</span>
      {row.old_value === undefined ? (
        <Badge variant="secondary" className="text-[10px]">
          新增
        </Badge>
      ) : (
        <span className="rounded bg-muted px-1.5 py-0.5 text-muted-foreground line-through decoration-destructive/60">
          {row.old_value}
        </span>
      )}
      <span aria-hidden className="text-muted-foreground">
        →
      </span>
      <span className="rounded bg-bubble-out px-1.5 py-0.5 font-medium text-bubble-out-foreground">
        {row.new_value}
      </span>
    </div>
  );
}

function FieldDiffList({ rows }: { rows: FieldDiff[] }) {
  if (rows.length === 0) return null;
  return (
    <div className="space-y-1.5">
      {rows.map((row, i) => (
        <FieldRow key={i} row={row} />
      ))}
    </div>
  );
}

/** 计划/档案草稿：Diff 呈现，组数行支持行内纠错（写回待确认草稿，不自动提交） */
function EditablePlanDiff({
  rows,
  disabled,
  onChange,
}: {
  rows: FieldDiff[];
  disabled: boolean;
  onChange: (rows: FieldDiff[]) => void;
}) {
  return (
    <div className="space-y-1.5">
      {rows.map((row, i) => {
        const editable = !disabled && row.field.includes("组数");
        if (!editable) return <FieldRow key={i} row={row} />;
        const value = row.new_value.match(/\d+/)?.[0] ?? "";
        return (
          <div key={i} className="flex flex-wrap items-center gap-2 text-xs">
            <span className="min-w-32 text-muted-foreground">{row.field}</span>
            {row.old_value !== undefined && (
              <span className="rounded bg-muted px-1.5 py-0.5 text-muted-foreground line-through decoration-destructive/60">
                {row.old_value}
              </span>
            )}
            <span aria-hidden className="text-muted-foreground">
              →
            </span>
            <Input
              type="number"
              min={0}
              value={value}
              onChange={(e) =>
                onChange(
                  rows.map((r, j) =>
                    j === i
                      ? {
                          ...r,
                          new_value:
                            e.target.value === "" ? "" : `${e.target.value} 组`,
                        }
                      : r,
                  ),
                )
              }
              className="h-7 w-16 text-xs"
              aria-label={`${row.field}（组）`}
            />
            <span className="text-muted-foreground">组</span>
          </div>
        );
      })}
    </div>
  );
}

/** 记录草稿：原始 payload vs 纠错后 payload 的字段级 Diff */
function recordDiffRows(
  base: RecordDraftPayload,
  current: RecordDraftPayload,
): FieldDiff[] {
  const rows: FieldDiff[] = [];
  if (base.date !== current.date)
    rows.push({
      field: "训练日期",
      old_value: base.date,
      new_value: current.date,
    });
  base.sets.forEach((b, i) => {
    const c = current.sets[i];
    if (!c) return;
    if (b.weight_kg !== c.weight_kg)
      rows.push({
        field: `第 ${i + 1} 组 · 重量（kg）`,
        old_value: fmt(b.weight_kg),
        new_value: fmt(c.weight_kg),
      });
    if (b.reps !== c.reps)
      rows.push({
        field: `第 ${i + 1} 组 · 次数`,
        old_value: fmt(b.reps),
        new_value: fmt(c.reps),
      });
    if (b.rir !== c.rir)
      rows.push({
        field: `第 ${i + 1} 组 · RIR`,
        old_value: fmt(b.rir, "未报告"),
        new_value: fmt(c.rir, "未报告"),
      });
  });
  return rows;
}

/* ------------------------------ 可编辑字段 -------------------------------- */

const toNumber = (v: string): number | undefined => {
  if (v.trim() === "") return undefined;
  const n = Number(v);
  return Number.isFinite(n) ? n : undefined;
};

function SetInputs({
  sets,
  disabled,
  onChange,
}: {
  sets: RecordSet[];
  disabled: boolean;
  onChange: (sets: RecordSet[]) => void;
}) {
  const patch = (i: number, p: Partial<RecordSet>) =>
    onChange(sets.map((s, j) => (j === i ? { ...s, ...p } : s)));
  return (
    <div className="space-y-1.5">
      {sets.map((s, i) => (
        <div key={i} className="flex flex-wrap items-center gap-1.5 text-xs">
          <span className="w-12 text-muted-foreground">第 {i + 1} 组</span>
          <Badge
            variant={s.set_type === "warmup" ? "secondary" : "outline"}
            className="text-[10px]"
          >
            {s.set_type === "warmup" ? "热身" : "工作"}
          </Badge>
          {s.assisted && (
            <Badge variant="secondary" className="text-[10px]">
              辅助
            </Badge>
          )}
          <Input
            type="number"
            min={0}
            value={s.weight_kg ?? ""}
            placeholder="重量"
            disabled={disabled}
            onChange={(e) => patch(i, { weight_kg: toNumber(e.target.value) })}
            className="h-7 w-20 text-xs"
            aria-label={`第 ${i + 1} 组重量（kg）`}
          />
          <span className="text-muted-foreground">kg ×</span>
          <Input
            type="number"
            min={0}
            value={s.reps ?? ""}
            placeholder="次数"
            disabled={disabled}
            onChange={(e) => patch(i, { reps: toNumber(e.target.value) })}
            className="h-7 w-16 text-xs"
            aria-label={`第 ${i + 1} 组次数`}
          />
          <span className="text-muted-foreground">次 · RIR</span>
          <Input
            type="number"
            min={0}
            step="0.5"
            value={s.rir ?? ""}
            placeholder="未报告"
            disabled={disabled}
            onChange={(e) => patch(i, { rir: toNumber(e.target.value) })}
            className="h-7 w-16 text-xs"
            aria-label={`第 ${i + 1} 组 RIR`}
          />
        </div>
      ))}
    </div>
  );
}

/* -------------------------------- 草稿卡 ---------------------------------- */

export interface DraftCardProps {
  draft: Draft;
  /** 当前（可能已被内联纠错）的草稿内容 */
  payload: DraftPayload;
  onChange: (payload: DraftPayload) => void;
  onConfirm: () => void;
  onRecalc: () => void;
  confirmPending: boolean;
  recalcPending: boolean;
  /** 确认返回 409 draft_stale 后的错误态 */
  staleError: boolean;
  /** 重算产生的新草稿：与旧草稿的 Diff */
  recalcDiff?: FieldDiff[];
  /** 已被重算草稿取代的旧卡 */
  superseded: boolean;
  /** 内联纠错提交：把纠错后的完整 payload 交给 revise 业务接口（01 1.2，不自动提交） */
  onRevise?: (payload: DraftPayload) => void;
  /** 丢弃待确认草稿（01 1.3：Discarded 不得再提交） */
  onDiscard?: () => void;
  /** 纠错进行中：按钮据此禁用 */
  revisePending?: boolean;
  /** 丢弃进行中：按钮据此禁用 */
  discardPending?: boolean;
}

export function DraftCard({
  draft,
  payload,
  onChange,
  onConfirm,
  onRecalc,
  confirmPending,
  recalcPending,
  staleError,
  recalcDiff,
  superseded,
  onRevise,
  onDiscard,
  revisePending = false,
  discardPending = false,
}: DraftCardProps) {
  const committed = draft.status === "committed";
  const discarded = draft.status === "discarded";

  if (superseded) {
    return (
      <div className="mt-3 flex items-center gap-2 rounded-xl border border-dashed bg-card/60 p-3 text-xs text-muted-foreground">
        <Badge variant="secondary">已过期</Badge>
        已由按最新数据重算的新草稿取代
      </div>
    );
  }

  // 内联纠错未提交（01 1.2：纠错不自动提交，须经「提交纠错」走业务接口）
  const dirty = JSON.stringify(payload) !== JSON.stringify(draft.payload);

  // 记录草稿：有纠错时用计算出的 Diff（反映纠错），否则展示服务端给的变更摘要
  const editedRows =
    "sets" in payload
      ? recordDiffRows(draft.payload as RecordDraftPayload, payload)
      : [];
  const diffRows: FieldDiff[] =
    "sets" in payload
      ? editedRows.length > 0
        ? editedRows
        : draft.diff
      : payload.diff;

  return (
    <div className="mt-3 rounded-xl border bg-card p-4 shadow-sm">
      {/* 头部：类型 + 状态 */}
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-sm font-medium">
          <Sparkles className="size-4 text-muted-foreground" aria-hidden />
          {KIND_LABEL[draft.kind]}
          {draft.parent_draft_id !== undefined && (
            <Badge variant="outline" className="text-[10px]">
              重算草稿
            </Badge>
          )}
        </div>
        <Badge
          variant={committed ? "default" : discarded ? "outline" : "secondary"}
          className={discarded ? "text-muted-foreground" : undefined}
        >
          {committed ? "已采纳" : discarded ? "已丢弃" : "待确认"}
        </Badge>
      </div>

      {/* 内容 + 内联纠错 */}
      {"sets" in payload ? (
        <div className="mt-3 space-y-2">
          <div className="rounded-lg bg-bubble-out px-3 py-2 text-xs text-bubble-out-foreground">
            <p className="font-medium">+ 新增训练记录</p>
            <p>
              {payload.date} · {payload.exercise}（{payload.variant}）
              {payload.warmup_summary
                ? ` · 热身：${payload.warmup_summary}`
                : ""}
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
            日期
            <Input
              type="date"
              value={payload.date}
              disabled={committed || discarded}
              onChange={(e) => onChange({ ...payload, date: e.target.value })}
              className="h-7 w-36 text-xs"
              aria-label="训练日期"
            />
          </div>
          <SetInputs
            sets={payload.sets}
            disabled={committed || discarded}
            onChange={(sets) => onChange({ ...payload, sets })}
          />
        </div>
      ) : (
        <div className="mt-3 space-y-2">
          <p className="text-sm font-medium">{payload.title}</p>
          <EditablePlanDiff
            rows={payload.diff}
            disabled={committed || discarded}
            onChange={(diff) => onChange({ ...payload, diff })}
          />
          <p className="text-[11px] text-muted-foreground">组数可内联纠错</p>
        </div>
      )}

      {/* 变更 Diff（A4） */}
      <div className="mt-3 border-t border-border pt-2">
        <p className="mb-1.5 text-xs font-medium text-muted-foreground">
          变更 Diff{editedRows.length > 0 ? "（已按纠错更新）" : ""}
        </p>
        <FieldDiffList rows={diffRows} />
      </div>

      {/* 重算草稿：新旧对比，须再次确认 */}
      {draft.parent_draft_id !== undefined && (
        <div className="mt-3 rounded-lg border border-border bg-muted/40 p-3">
          <p className="text-xs font-medium">
            已按最新数据重算，请再次确认后生效
          </p>
          {recalcDiff && recalcDiff.length > 0 ? (
            <>
              <p className="mb-1.5 mt-0.5 text-[11px] text-muted-foreground">
                与旧草稿对比：
              </p>
              <FieldDiffList rows={recalcDiff} />
            </>
          ) : (
            <p className="mt-0.5 text-[11px] text-muted-foreground">
              重算后内容与旧草稿一致。
            </p>
          )}
        </div>
      )}

      {/* draft_stale 错误态（5d） */}
      {staleError && !committed && (
        <div className="mt-3 flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-xs">
          <CircleAlert
            className="mt-0.5 size-4 shrink-0 text-destructive"
            aria-hidden
          />
          <div className="flex-1">
            <p className="font-medium text-destructive">
              业务数据已变更，此草稿无法直接确认
            </p>
            <p className="mt-0.5 text-muted-foreground">
              请按最新数据一键重算，生成新草稿并再次确认。
            </p>
            <Button
              variant="outline"
              size="sm"
              className="mt-2"
              onClick={onRecalc}
              disabled={recalcPending}
            >
              <RefreshCcw aria-hidden />
              按最新数据一键重算
            </Button>
          </div>
        </div>
      )}

      {/* 操作区：纠错不替代最终确认 */}
      {!committed && !discarded && !staleError && (
        <div className="mt-4 flex items-center justify-end gap-2">
          {dirty && (
            <span className="mr-auto text-[11px] text-muted-foreground">
              先提交纠错（确认前需保存修改）
            </span>
          )}
          {onRevise && dirty && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => onRevise(payload)}
              disabled={revisePending}
            >
              提交纠错
            </Button>
          )}
          {/* 有未提交的内联纠错时，确认前必须先走「提交纠错」（01 1.2） */}
          <Button
            size="sm"
            onClick={onConfirm}
            disabled={confirmPending || dirty}
          >
            确认采纳
          </Button>
          {onDiscard && (
            <Button
              variant="outline"
              size="sm"
              onClick={onDiscard}
              disabled={discardPending}
            >
              丢弃草稿
            </Button>
          )}
        </div>
      )}

      {/* 已丢弃（01 1.3）：不得再提交 */}
      {discarded && (
        <p className="mt-3 text-[11px] text-muted-foreground">
          草稿已丢弃，不可确认。
        </p>
      )}
    </div>
  );
}
