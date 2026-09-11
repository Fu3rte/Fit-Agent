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
  Draft,
  DraftKind,
  DraftExerciseLog,
  DraftPayload,
  FieldDiff,
  ProfileDraftPayload,
  RecordDraftPayload,
} from "@/lib/contract";
import { deriveRangeLabel, prescriptionLabel } from "@/lib/planView";
import { profilePayloadDiff } from "@/lib/profile";
import { FieldDiffList } from "./draftFields";
import { PlanDraftFields } from "./PlanDraftFields";
import { ProfileDraftFields } from "./ProfileDraftFields";
import { SetInputs } from "./SetInputs";

const KIND_LABEL: Record<DraftKind, string> = {
  training_record: "训练记录草稿",
  plan: "计划调整草稿",
  profile_update: "档案变更草稿",
  arrangement: "当次安排草稿",
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
      if (bs.rir !== cs.rir)
        rows.push({
          field: `动作 ${ei + 1} 第 ${i + 1} 组 · RIR`,
          old_value: fmt(bs.rir, "未报告"),
          new_value: fmt(cs.rir, "未报告"),
        });
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
  recalcDiff?: FieldDiff[];
  superseded: boolean;
  onRevise?: (payload: DraftPayload) => void;
  onDiscard?: () => void;
  revisePending?: boolean;
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

  const dirty = JSON.stringify(payload) !== JSON.stringify(draft.payload);

  const isRecord = "occurred_on" in payload;
  const isProfile = "profile" in payload;
  const isArrangement = "target" in payload;
  const isPlan = "diff" in payload && "title" in payload;

  const editedRows = isRecord
    ? recordDiffRows(draft.payload as RecordDraftPayload, payload)
    : isProfile
      ? profilePayloadDiff(draft.payload as ProfileDraftPayload, payload)
      : [];
  const diffRows: FieldDiff[] =
    isRecord || isProfile
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

      {/* 记录草稿：多动作 SetFacts */}
      {isRecord && (
        <div className="mt-3 space-y-2">
          {(() => {
            const p = payload as RecordDraftPayload;
            return (
              <>
                <div className="rounded-lg bg-bubble-out px-3 py-2 text-xs text-bubble-out-foreground">
                  <p className="font-medium">
                    + {p.training_session_id ? "补充/更正训练记录" : "新增训练记录"}
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

      {/* 安排草稿：完整目标 + 差异 */}
      {isArrangement && (
        <div className="mt-3 space-y-2">
          {(() => {
            const p = payload as ArrangementDraftPayload;
            return (
              <>
                <div className="rounded-lg border border-border bg-muted/30 p-2.5 text-xs">
                  <p className="font-medium">
                    当次安排 · {p.target.scheduled_on} · {p.target.plan_workout_key}
                    （计划 {p.target.plan_version}）
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
                          ` · RIR ${deriveRangeLabel(e.prescription.target_rir)}`}
                      </li>
                    ))}
                  </ul>
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

/** 单动作记录编辑器：组列表（SetFacts）+ 组类型/负重/次数/RIR */
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

