/**
 * 草稿卡（PLAN-FRONTEND「草稿卡必含元素」）：
 * - 结构化字段级 Diff（A4）：旧值→新值，无旧值标「新增」；
 * - 关键字段内联纠错：只改待确认草稿（受控 payload），Diff 与展示实时更新，不自动提交；
 * - 确认采纳幂等；409 draft_stale → 错误态 + 按最新数据一键重算；
 * - 丢弃待确认草稿（01 1.3）。
 * - 记录草稿对齐 S3-10 多动作 SetFacts；安排草稿展示完整目标与差异（S3-08）。
 */
import { CircleAlert, RefreshCcw, Sparkles } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type {
  ArrangementDraftPayload,
  ArrangementItemDisposition,
  Draft,
  DraftKind,
  DraftExerciseLog,
  DraftPayload,
  FieldDiff,
  PlanExerciseItem,
  ProfileDraftPayload,
  RecordDraftPayload,
} from "@/lib/contract";
import {
  deriveRangeLabel,
  effortPlainLabel,
  prescriptionLabel,
} from "@/lib/planView";
import { profilePayloadDiff } from "@/lib/profile";
import { FieldDiffList } from "./draftFields";
import { PlanDraftFields } from "./PlanDraftFields";
import { ProfileDraftFields } from "./ProfileDraftFields";
import { SetInputs } from "./SetInputs";

/**
 * 真实后端 kind 封闭集标签（backend DRAFT_KINDS 恰四种）：
 * profile_update | plan | training_record | arrangement。
 * F6-02d：training_void / MOCK_ONLY_KIND_LABEL 已随 mock 删除。
 */
const KIND_LABEL: Record<DraftKind, string> = {
  training_record: "训练记录草稿",
  plan: "计划调整草稿",
  profile_update: "档案变更草稿",
  arrangement: "当次安排草稿",
};

const kindLabel = (kind: DraftKind): string =>
  KIND_LABEL[kind] ?? "训练草稿";

const DISPOSITION_BADGE: Record<ArrangementItemDisposition, string> = {
  keep: "保留 · 目标更保守",
  deload: "减载",
  equivalent_replace: "同等刺激替换",
  local_skip: "局部跳过",
};

const fmt = (v: number | string | undefined | null, fallback = "—") =>
  v === undefined || v === null || v === "" ? fallback : String(v);

/** 记录草稿：payload vs 纠错后 payload 的字段级 Diff */
function recordDiffRows(
  base: RecordDraftPayload,
  current: RecordDraftPayload,
): FieldDiff[] {
  const rows: FieldDiff[] = [];
  if (base.occurred_on !== current.occurred_on)
    rows.push({
      field: "训练日期",
      old_value: base.occurred_on,
      new_value: current.occurred_on,
    });
  base.exercises.forEach((b, ei) => {
    const c = current.exercises[ei];
    if (!c) return;
    b.sets.forEach((bs, i) => {
      const cs = c.sets[i];
      if (!cs) return;
      if (bs.load?.value_text !== cs.load?.value_text)
        rows.push({
          field: `动作 ${ei + 1} 第 ${i + 1} 组 · 重量`,
          old_value: fmt(bs.load?.value_text),
          new_value: fmt(cs.load?.value_text),
        });
      if (bs.reps !== cs.reps)
        rows.push({
          field: `动作 ${ei + 1} 第 ${i + 1} 组 · 次数`,
          old_value: fmt(bs.reps),
          new_value: fmt(cs.reps),
        });
    });
  });
  return rows;
}

/** 安排草稿：纠错后 payload vs 原草稿的字段级 Diff（与服务端派生口径对齐） */
function arrangementLocalDiff(
  base: ArrangementDraftPayload,
  current: ArrangementDraftPayload,
): FieldDiff[] {
  const rows: FieldDiff[] = [];
  const b = base.target;
  const c = current.target;
  if (b.adjustment_reason !== c.adjustment_reason)
    rows.push({
      field: "调整原因",
      old_value: fmt(b.adjustment_reason),
      new_value: fmt(c.adjustment_reason),
    });
  b.exercises.forEach((be, ei) => {
    const ce = c.exercises[ei];
    if (!ce || be.prescription.kind !== "reps" || ce.prescription.kind !== "reps")
      return;
    const name = ce.display_snapshot.name;
    if (be.prescription.work_sets !== ce.prescription.work_sets)
      rows.push({
        field: `${name} · 组数`,
        old_value: `${be.prescription.work_sets} 组`,
        new_value: `${ce.prescription.work_sets} 组`,
      });
    if (
      JSON.stringify(be.prescription.reps_range) !==
      JSON.stringify(ce.prescription.reps_range)
    )
      rows.push({
        field: `${name} · 每组次数`,
        old_value: deriveRangeLabel(be.prescription.reps_range),
        new_value: deriveRangeLabel(ce.prescription.reps_range),
      });
    if (
      JSON.stringify(be.prescription.target_rir) !==
      JSON.stringify(ce.prescription.target_rir)
    )
      rows.push({
        field: `${name} · 目标用力`,
        old_value: effortPlainLabel(be.prescription.target_rir),
        new_value: effortPlainLabel(ce.prescription.target_rir),
      });
  });
  return rows;
}

/* -------------------------------- 草稿卡 ---------------------------------- */

export interface DraftCardProps {
  draft: Draft;
  payload: DraftPayload;
  onChange: (payload: DraftPayload) => void;
  onConfirm: () => void;
  onRecalc: () => void;
  confirmPending: boolean;
  recalcPending: boolean;
  staleError: boolean;
  /** draft_stale 冲突时的变更项说明（如「context_version 已从…推进」） */
  staleDetail?: string;
  recalcDiff?: FieldDiff[];
  superseded: boolean;
  onRevise?: (payload: DraftPayload) => void;
  onDiscard?: () => void;
  revisePending?: boolean;
  discardPending?: boolean;
  /** 作废整次训练（POST /api/drafts/{id}/void）：仅绑定既有身份的待确认记录草稿 */
  onVoid?: () => void;
  voidPending?: boolean;
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
  staleDetail,
  recalcDiff,
  superseded,
  onRevise,
  onDiscard,
  revisePending = false,
  discardPending = false,
  onVoid,
  voidPending = false,
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

  const dirty = JSON.stringify(payload) !== JSON.stringify(draft.payload);

  const isRecord = "occurred_on" in payload;
  const isProfile = "profile" in payload;
  const isArrangement = "target" in payload;
  const isPlan = "diff" in payload && "title" in payload;
  /**
   * 作废入口（F6-02c 已拍）：真实后端无独立 training_void kind——作废经
   * POST /api/drafts/{id}/void 对绑定既有身份的训练记录草稿追加 voided 修订。
   * 仅待确认 + 已绑定身份（training_session_id 非空）时展示；新记录无身份可作废。
   */
  const canVoid =
    draft.kind === "training_record" &&
    !committed &&
    !discarded &&
    onVoid !== undefined &&
    isRecord &&
    Boolean((payload as RecordDraftPayload).training_session_id);

  const editedRows = isRecord
    ? recordDiffRows(draft.payload as RecordDraftPayload, payload)
    : isProfile
      ? profilePayloadDiff(draft.payload as ProfileDraftPayload, payload)
      : isArrangement
        ? arrangementLocalDiff(
            draft.payload as ArrangementDraftPayload,
            payload,
          )
        : [];
  const diffRows: FieldDiff[] =
    isRecord || isProfile || isArrangement
      ? editedRows.length > 0
        ? editedRows
        : draft.diff
      : isPlan
        ? (payload as { diff: FieldDiff[] }).diff
        : draft.diff;

  return (
    <div className="mt-3 rounded-xl border bg-card p-4 shadow-sm">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-sm font-medium">
          <Sparkles className="size-4 text-muted-foreground" aria-hidden />
          {kindLabel(draft.kind)}
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

      {/* 记录草稿：多动作 SetFacts */}
      {isRecord && (
        <div className="mt-3 space-y-2">
          {(() => {
            const p = payload as RecordDraftPayload;
            return (
              <>
                <div className="rounded-lg bg-bubble-out px-3 py-2 text-xs text-bubble-out-foreground">
                  <p className="font-medium">
                    +{" "}
                    {p.training_session_id
                      ? "更正训练记录（不新增训练身份）"
                      : "新增训练记录"}
                  </p>
                  <p>
                    {p.occurred_on}
                    {p.training_session_id
                      ? ` · 身份 ${p.training_session_id}`
                      : " · 新身份（确认时建立）"}
                    {p.exercises.length > 0
                      ? ` · ${p.exercises.length} 个动作`
                      : ""}
                  </p>
                </div>
                {/* 对照摘要（F3-04）：显式携带安排时展示「原计划 X 组 · 当次安排 Y 组」；无安排不显示 */}
                {p.arrangement_revision_id && (
                  <p className="text-[11px] text-muted-foreground">
                    对照：
                    {draft.diff
                      .filter((r) => r.field.startsWith("对照"))
                      .map((r) => r.new_value)
                      .join(" · ") || "—"}
                  </p>
                )}
                <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
                  日期
                  <Input
                    type="date"
                    value={p.occurred_on}
                    disabled={committed || discarded}
                    onChange={(e) =>
                      onChange({ ...p, occurred_on: e.target.value })
                    }
                    className="h-7 w-36 text-xs"
                    aria-label="训练日期"
                  />
                  训练身份
                  <Input
                    value={p.training_session_id ?? ""}
                    placeholder="空 = 新增"
                    disabled={committed || discarded}
                    onChange={(e) =>
                      onChange({
                        ...p,
                        training_session_id: e.target.value || null,
                      })
                    }
                    className="h-7 w-36 text-xs"
                    aria-label="既有训练身份 id（空 = 新增）"
                  />
                </div>
                {p.exercises.map((ex, ei) => (
                  <RecordExerciseEditor
                    key={ex.position}
                    exercise={ex}
                    index={ei}
                    disabled={committed || discarded}
                    onChange={(next) =>
                      onChange({
                        ...p,
                        exercises: p.exercises.map((e, j) =>
                          j === ei ? next : e,
                        ),
                      })
                    }
                  />
                ))}
              </>
            );
          })()}
        </div>
      )}

      {/* 安排草稿：完整目标 + 可纠错（F3-02：deload/keep 主路径 + 原因输入） */}
      {isArrangement && (
        <div className="mt-3 space-y-2">
          {(() => {
            const p = payload as ArrangementDraftPayload;
            const editable = !committed && !discarded;
            return (
              <>
                <div className="rounded-lg border border-border bg-muted/30 p-2.5 text-xs">
                  <p className="font-medium">
                    当次安排 · {p.target.scheduled_on} ·{" "}
                    {p.target.plan_workout_key}（计划 {p.target.plan_version}）
                  </p>
                  {p.target.adjustment_reason && (
                    <p className="mt-1 text-muted-foreground">
                      原因：{p.target.adjustment_reason}
                    </p>
                  )}
                  <ul className="mt-1.5 space-y-0.5">
                    {p.target.exercises.map((e) => (
                      <li key={e.item_key}>
                        {e.display_snapshot.name} ·{" "}
                        {prescriptionLabel(e.prescription)}
                        {e.prescription.kind === "reps" &&
                          e.prescription.target_rir &&
                          ` · ${effortPlainLabel(e.prescription.target_rir)}`}
                        {e.disposition && (
                          <Badge
                            variant="secondary"
                            className="ml-1.5 align-middle text-[10px]"
                          >
                            {DISPOSITION_BADGE[e.disposition]}
                          </Badge>
                        )}
                      </li>
                    ))}
                  </ul>
                </div>
                {/* 内联纠错：组数 / 目标用力 / 原因（仅待确认可编辑；终态只读） */}
                <div className="space-y-1.5">
                  <p className="text-[11px] font-medium text-muted-foreground">
                    内联纠错（确认前需提交纠错）
                  </p>
                  <ArrangementExerciseEditors
                    payload={p}
                    disabled={!editable}
                    onChange={onChange}
                  />
                  <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
                    调整原因
                    <Input
                      value={p.target.adjustment_reason ?? ""}
                      disabled={!editable}
                      onChange={(e) =>
                        onChange({
                          ...p,
                          target: {
                            ...p.target,
                            adjustment_reason: e.target.value,
                          },
                        })
                      }
                      className="h-7 flex-1 text-xs"
                      aria-label="调整原因"
                      placeholder="必填：说明为何调整"
                    />
                  </label>
                </div>
                <p className="text-[11px] text-muted-foreground">
                  确认后写入当次安排修订；不修改长期计划与其余日程。
                </p>
              </>
            );
          })()}
        </div>
      )}

      {isProfile && (
        <ProfileDraftFields
          payload={payload}
          disabled={committed || discarded}
          onChange={onChange}
        />
      )}
      {isPlan && (
        <div className="mt-3 space-y-2">
          <p className="text-sm font-medium">
            {(payload as { title: string }).title}
          </p>
          <PlanDraftFields
            payload={payload as never}
            disabled={committed || discarded}
            onChange={onChange}
          />
        </div>
      )}

      <div className="mt-3 border-t border-border pt-2">
        <p className="mb-1.5 text-xs font-medium text-muted-foreground">
          变更 Diff
          {editedRows.length > 0
            ? "（已按纠错更新）"
            : isPlan && dirty
              ? "（提交纠错后由服务端按修改后内容重算）"
              : ""}
        </p>
        <FieldDiffList rows={diffRows} />
      </div>

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
            {staleDetail && (
              <p className="mt-0.5 text-muted-foreground">
                相关变更：{staleDetail}
              </p>
            )}
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
              按最新数据重新生成草稿
            </Button>
          </div>
        </div>
      )}

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
          <Button
            size="sm"
            onClick={onConfirm}
            disabled={confirmPending || dirty}
          >
            确认采纳
          </Button>
          {canVoid && (
            <Button
              variant="outline"
              size="sm"
              onClick={onVoid}
              disabled={voidPending}
              title="作废整次训练：追加 voided 修订并退出统计（不物理删除）"
            >
              作废该次训练
            </Button>
          )}
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

      {discarded && (
        <p className="mt-3 text-[11px] text-muted-foreground">
          草稿已丢弃，不可确认。
        </p>
      )}
    </div>
  );
}

/** 单动作记录编辑器：组列表（SetFacts）+ 组类型/负重/次数/余力 */
function RecordExerciseEditor({
  exercise,
  index,
  disabled,
  onChange,
}: {
  exercise: DraftExerciseLog;
  index: number;
  disabled: boolean;
  onChange: (next: DraftExerciseLog) => void;
}) {
  return (
    <div className="rounded-lg border border-border bg-card/60 p-2.5">
      <p className="text-xs font-medium">
        动作 {index + 1} · {exercise.exercise_id}
        <span className="ml-1.5 text-muted-foreground">
          {exercise.record_type}
        </span>
      </p>
      {exercise.warmup_summary_text && (
        <p className="mt-0.5 text-[11px] text-muted-foreground">
          热身：{exercise.warmup_summary_text}
        </p>
      )}
      <SetInputs
        sets={exercise.sets}
        disabled={disabled}
        onChange={(sets) => onChange({ ...exercise, sets })}
      />
    </div>
  );
}

/**
 * 安排草稿内联纠错（F3-02 主路径）：
 * - deload：只可减 work_sets；
 * - keep：只可升 target_rir（min/max）；
 * - local_skip / equivalent_replace / 计时型：目标保持只读展示。
 * 不在此提交，统一走「提交纠错」→ revise（01 1.2 单一编辑入口）。
 */
function ArrangementExerciseEditors({
  payload,
  disabled,
  onChange,
}: {
  payload: ArrangementDraftPayload;
  disabled: boolean;
  onChange: (payload: DraftPayload) => void;
}) {
  const updateItem = (index: number, next: PlanExerciseItem) => {
    onChange({
      ...payload,
      target: {
        ...payload.target,
        exercises: payload.target.exercises.map((e, i) =>
          i === index ? next : e,
        ),
      },
    });
  };
  return (
    <div className="space-y-1.5">
      {payload.target.exercises.map((e, ei) => {
        if (e.prescription.kind !== "reps") return null;
        const rx = e.prescription;
        const d = e.disposition;
        const canEditSets = d === "deload" || d === undefined;
        const canEditRir = d === "keep" || d === undefined;
        return (
          <div
            key={e.item_key}
            className="flex flex-wrap items-center gap-1.5 rounded-lg border border-border bg-card/60 px-2.5 py-1.5 text-xs"
          >
            <span className="font-medium">{e.display_snapshot.name}</span>
            {d && (
              <Badge variant="secondary" className="text-[10px]">
                {DISPOSITION_BADGE[d]}
              </Badge>
            )}
            {canEditSets && (
              <label className="flex items-center gap-1 text-muted-foreground">
                组数
                <Input
                  type="number"
                  min={1}
                  value={rx.work_sets}
                  disabled={disabled}
                  onChange={(ev) => {
                    const v = Number(ev.target.value);
                    if (!Number.isFinite(v) || v < 1) return;
                    updateItem(ei, {
                      ...e,
                      prescription: {
                        kind: "reps",
                        work_sets: Math.floor(v),
                        reps_range: rx.reps_range,
                        ...(rx.target_rir ? { target_rir: rx.target_rir } : {}),
                      },
                    });
                  }}
                  className="h-7 w-16 text-xs"
                  aria-label={`${e.display_snapshot.name} 组数`}
                />
              </label>
            )}
            {canEditRir && rx.target_rir && (
              <label className="flex items-center gap-1 text-muted-foreground">
                目标用力（还能再做）
                <Input
                  type="number"
                  min={0}
                  value={rx.target_rir.min}
                  disabled={disabled}
                  onChange={(ev) => {
                    const v = Number(ev.target.value);
                    if (!Number.isFinite(v) || v < 0) return;
                    const rir = rx.target_rir;
                    if (!rir) return;
                    updateItem(ei, {
                      ...e,
                      prescription: {
                        kind: "reps",
                        work_sets: rx.work_sets,
                        reps_range: rx.reps_range,
                        target_rir: {
                          min: Math.floor(v),
                          max: Math.max(Math.floor(v), rir.max),
                        },
                      },
                    });
                  }}
                  className="h-7 w-16 text-xs"
                  aria-label={`${e.display_snapshot.name} 目标用力下限`}
                />
                –
                <Input
                  type="number"
                  min={0}
                  value={rx.target_rir.max}
                  disabled={disabled}
                  onChange={(ev) => {
                    const v = Number(ev.target.value);
                    if (!Number.isFinite(v) || v < 0) return;
                    const rir = rx.target_rir;
                    if (!rir) return;
                    updateItem(ei, {
                      ...e,
                      prescription: {
                        kind: "reps",
                        work_sets: rx.work_sets,
                        reps_range: rx.reps_range,
                        target_rir: {
                          min: Math.min(rir.min, Math.floor(v)),
                          max: Math.floor(v),
                        },
                      },
                    });
                  }}
                  className="h-7 w-16 text-xs"
                  aria-label={`${e.display_snapshot.name} 目标用力上限`}
                />
              </label>
            )}
            {!canEditSets && !canEditRir && (
              <span className="text-muted-foreground">
                {d === "local_skip"
                  ? "局部跳过：目标保持计划值"
                  : d === "equivalent_replace"
                    ? "同等刺激替换：处方照抄计划"
                    : "按处置只读展示"}
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
}

