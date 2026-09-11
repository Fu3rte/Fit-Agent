import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import type {
  DraftPayload,
  PlanBlock,
  PlanDraftPayload,
  PlanExercise,
  PlanScheduleEntry,
  PlanScope,
} from "@/lib/contract";
import { FieldLine, selectClass } from "./draftFields";

/** 每周训练日展示文案（1-7，周一起；与档案页一致） */
const WEEKDAY_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

const SCHEDULE_STATUS_LABEL: Record<PlanScheduleEntry["status"], string> = {
  scheduled: "应训练",
  locked: "已锁定",
  cancelled: "已取消",
};

const scheduleLine = (s: PlanScheduleEntry) =>
  `${s.date}（${WEEKDAY_LABELS[s.weekday - 1] ?? s.weekday}）· ${SCHEDULE_STATUS_LABEL[s.status]}`;

/**
 * 计划草稿结构化卡（F2-03）：计划版本、生效范围、每周安排、动作顺序与处方（组数／次数
 * 区间／目标 RIR／渐进）、校准说明与具体日程。替换时旧版未锁定日程的取消清单不进产品 UI
 * 展示（owner 2026-09-10 呈现覆盖；取消事务与载荷不变）。
 * 轻量纠错只改待确认草稿（开始／复核日期、训练日、动作候选、组数、次数区间、目标 RIR），
 * 仍经「提交纠错」走 revise 使 revision+1，不自动提交、不改正式计划；纠错后的预计时长、
 * 具体日程与展示 Diff 一律由服务端按修改后内容重算（本卡不复制排程算法），
 * 因此提交前这里的日程仍是上一次服务端结果。
 */
export function PlanDraftFields({
  payload,
  disabled,
  onChange,
}: {
  payload: PlanDraftPayload;
  disabled: boolean;
  onChange: (payload: DraftPayload) => void;
}) {
  const plan = payload.plan;
  const scope = payload.scope;
  if (!plan || !scope) {
    // 无结构化载荷的旧计划草稿（F2-04 替换流程接管）：只展示标题与服务端 Diff
    return (
      <p className="text-[11px] text-muted-foreground">
        本草稿未携带结构化计划载荷，仅展示服务端变更 Diff。
      </p>
    );
  }
  const candidates = payload.candidates ?? [];
  const schedules = payload.schedules ?? [];
  /* 校准说明：本阶段无可信训练记录，全部动作共一条（不给起始重量） */
  const calibration = plan.blocks.flatMap((b) => b.exercises)[0]?.calibration;

  /** 改生效范围：同时同步计划版本头；服务端纠错时仍会重新对齐 */
  const patchDates = (
    p: Partial<Pick<PlanScope, "start_date" | "review_date">>,
  ) =>
    onChange({
      ...payload,
      scope: { ...scope, ...p },
      plan: { ...plan, ...p },
    });
  const patchBlock = (bi: number, p: Partial<PlanBlock>) =>
    onChange({
      ...payload,
      plan: {
        ...plan,
        blocks: plan.blocks.map((b, j) => (j === bi ? { ...b, ...p } : b)),
      },
    });
  const patchExercise = (bi: number, ei: number, p: Partial<PlanExercise>) =>
    onChange({
      ...payload,
      plan: {
        ...plan,
        blocks: plan.blocks.map((b, j) =>
          j === bi
            ? {
                ...b,
                exercises: b.exercises.map((e, k) =>
                  k === ei ? { ...e, ...p } : e,
                ),
              }
            : b,
        ),
      },
    });
  /** 换动作：只提交目录身份，展示名随之取候选条目（服务端仍按目录重建身份与文案） */
  const pickExercise = (bi: number, ei: number, exercise_id: string) => {
    const candidate = candidates.find((c) => c.exercise_id === exercise_id);
    patchExercise(bi, ei, {
      exercise_id,
      ...(candidate
        ? { name: candidate.name, variant: candidate.variant }
        : {}),
    });
  };
  const weekdays = [...new Set(plan.blocks.map((b) => b.weekday))].sort(
    (a, b) => a - b,
  );

  return (
    <div className="mt-3 space-y-2.5 rounded-lg border border-border bg-muted/30 p-3">
      <p className="text-xs font-medium">
        计划版本 {plan.version}
        （拟议启用；确认前正式计划与日程不变）
      </p>

      <FieldLine label="开始日期">
        <Input
          type="date"
          value={scope.start_date}
          disabled={disabled}
          onChange={(e) => patchDates({ start_date: e.target.value })}
          className="h-7 w-36 text-xs"
          aria-label="计划开始日期"
        />
      </FieldLine>
      <FieldLine label="复核日期">
        <Input
          type="date"
          value={scope.review_date}
          disabled={disabled}
          onChange={(e) => patchDates({ review_date: e.target.value })}
          className="h-7 w-36 text-xs"
          aria-label="计划复核日期"
        />
      </FieldLine>
      <FieldLine label="每周安排">
        <span>
          {weekdays.map((w) => WEEKDAY_LABELS[w - 1] ?? w).join(" / ")}
          ，共 {schedules.length} 个应训练日（复核日当天不排）
        </span>
      </FieldLine>

      {/* 训练日安排：改板块星期即改训练日；日程、预计时长与 Diff 由服务端重算 */}
      {plan.blocks.map((block, bi) => (
        <div
          key={bi}
          className="rounded-lg border border-border bg-card/60 p-2.5"
        >
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <span className="font-medium">{block.name}</span>
            <select
              aria-label={`${block.name}的训练日`}
              className={selectClass}
              value={block.weekday}
              disabled={disabled}
              onChange={(e) =>
                patchBlock(bi, {
                  weekday: Number(e.target.value),
                })
              }
            >
              {WEEKDAY_LABELS.map((label, i) => (
                <option key={label} value={i + 1}>
                  {label}
                </option>
              ))}
            </select>
            <span className="text-muted-foreground">
              预计 {block.estimated_minutes} 分钟
            </span>
          </div>
          {block.exercises.map((ex, ei) => (
            <div
              key={ei}
              className="mt-1.5 flex flex-wrap items-center gap-1.5 text-xs"
            >
              <span className="w-5 text-muted-foreground">{ei + 1}.</span>
              <select
                aria-label={`${block.name}第 ${ei + 1} 个动作`}
                className={`${selectClass} max-w-56`}
                value={ex.exercise_id}
                disabled={disabled}
                onChange={(e) => pickExercise(bi, ei, e.target.value)}
              >
                {/* 目录身份为唯一身份；候选里没有的原动作也保留可选，不静默丢弃 */}
                {!candidates.some((c) => c.exercise_id === ex.exercise_id) && (
                  <option value={ex.exercise_id}>
                    {ex.name}（{ex.variant}
                    ，不在当前候选内）
                  </option>
                )}
                {candidates.map((c) => (
                  <option key={c.exercise_id} value={c.exercise_id}>
                    {c.name}（{c.variant}）
                  </option>
                ))}
              </select>
              <Input
                type="number"
                min={1}
                value={ex.sets}
                disabled={disabled}
                onChange={(e) =>
                  patchExercise(bi, ei, {
                    sets: Number(e.target.value),
                  })
                }
                className="h-7 w-14 text-xs"
                aria-label={`${ex.name}组数`}
              />
              <span className="text-muted-foreground">组 ×</span>
              <Input
                value={ex.rep_range}
                disabled={disabled}
                onChange={(e) =>
                  patchExercise(bi, ei, {
                    rep_range: e.target.value,
                  })
                }
                className="h-7 w-16 text-xs"
                aria-label={`${ex.name}次数区间`}
              />
              <span className="text-muted-foreground">次 · RIR</span>
              <Input
                value={ex.target_rir}
                disabled={disabled}
                onChange={(e) =>
                  patchExercise(bi, ei, {
                    target_rir: e.target.value,
                  })
                }
                className="h-7 w-16 text-xs"
                aria-label={`${ex.name}目标 RIR`}
              />
              <span className="text-muted-foreground">{ex.progression}</span>
              {ex.calibration.status === "needs_calibration" && (
                <Badge variant="secondary" className="text-[10px]">
                  需要校准
                </Badge>
              )}
            </div>
          ))}
        </div>
      ))}

      {calibration && (
        <div className="rounded-lg border border-dashed border-border p-2.5 text-xs">
          <p className="font-medium">
            校准说明（无可信训练记录：不给起始重量）
          </p>
          <ol className="mt-1 list-decimal space-y-0.5 pl-4 text-muted-foreground">
            {calibration.steps.map((step) => (
              <li key={step}>{step}</li>
            ))}
          </ol>
          <p className="mt-1 text-muted-foreground">
            通过：{calibration.pass_criteria}；停止：
            {calibration.stop_criteria}
          </p>
        </div>
      )}

      <div className="text-xs">
        <div className="mb-1.5 text-muted-foreground">
          具体日程（{scope.start_date} 起至复核日 {scope.review_date} 前）
        </div>
        {schedules.length === 0 ? (
          <p className="text-muted-foreground">尚无日程</p>
        ) : (
          <div className="flex max-h-40 flex-wrap gap-1 overflow-y-auto">
            {schedules.map((s) => (
              <span
                key={s.id}
                className="rounded bg-bubble-out px-1.5 py-0.5 text-[11px] text-bubble-out-foreground"
              >
                {scheduleLine(s)}
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
