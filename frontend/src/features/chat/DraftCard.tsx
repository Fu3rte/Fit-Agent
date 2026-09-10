/**
 * 草稿卡（PLAN-FRONTEND「草稿卡必含元素」）：
 * - 结构化字段级 Diff（A4）：旧值→新值，无旧值标「新增」；
 * - 关键字段内联纠错：只改待确认草稿（受控 payload），Diff 与展示实时更新，不自动提交；
 *   未提交的纠错经「提交纠错」走纠错业务接口（01 1.2），确认写入的是服务端最新草稿；
 * - 确认采纳幂等；409 draft_stale → 错误态 + 按最新数据一键重算；
 * - 重算产生的新草稿展示新旧草稿 Diff，再次确认才生效；
 * - 档案草稿：六类事实 + 必填体重结构化展示，选择/数字/文本与器械、限制列表增删走
 *   同一纠错接口（限制含粒度）；红旗症状只读（02 2.3：Agent 不解除红旗）；
 * - 丢弃待确认草稿（01 1.3）：已丢弃徽章 + 只读，不得再提交。
 */
import { useState, type ReactNode } from "react";
import { CircleAlert, RefreshCcw, Sparkles } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type {
    Draft,
    DraftKind,
    DraftPayload,
    FieldDiff,
    PlanBlock,
    PlanDraftPayload,
    PlanExercise,
    PlanScope,
    PlanScheduleEntry,
    Profile,
    ProfileDraftPayload,
    RecordDraftPayload,
    RecordSet,
    Restriction,
} from "@/lib/contract";
import { profilePayloadDiff } from "@/lib/profile";

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
 * 区间／目标 RIR／渐进）、校准说明、具体日程与替换取消预览。
 * 轻量纠错只改待确认草稿（开始／复核日期、训练日、动作候选、组数、次数区间、目标 RIR），
 * 仍经「提交纠错」走 revise 使 revision+1，不自动提交、不改正式计划；纠错后的预计时长、
 * 具体日程与展示 Diff 一律由服务端按修改后内容重算（本卡不复制排程算法），
 * 因此提交前这里的日程仍是上一次服务端结果。
 */
function PlanDraftFields({
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
    const cancellations = payload.cancellations ?? [];
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
                blocks: plan.blocks.map((b, j) =>
                    j === bi ? { ...b, ...p } : b,
                ),
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
                    onChange={(e) =>
                        patchDates({ review_date: e.target.value })
                    }
                    className="h-7 w-36 text-xs"
                    aria-label="计划复核日期"
                />
            </FieldLine>
            <FieldLine label="每周安排">
                <span>
                    {weekdays
                        .map((w) => WEEKDAY_LABELS[w - 1] ?? w)
                        .join(" / ")}
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
                            <span className="w-5 text-muted-foreground">
                                {ei + 1}.
                            </span>
                            <select
                                aria-label={`${block.name}第 ${ei + 1} 个动作`}
                                className={`${selectClass} max-w-56`}
                                value={ex.exercise_id}
                                disabled={disabled}
                                onChange={(e) =>
                                    pickExercise(bi, ei, e.target.value)
                                }
                            >
                                {/* 目录身份为唯一身份；候选里没有的原动作也保留可选，不静默丢弃 */}
                                {!candidates.some(
                                    (c) => c.exercise_id === ex.exercise_id,
                                ) && (
                                    <option value={ex.exercise_id}>
                                        {ex.name}（{ex.variant}
                                        ，不在当前候选内）
                                    </option>
                                )}
                                {candidates.map((c) => (
                                    <option
                                        key={c.exercise_id}
                                        value={c.exercise_id}
                                    >
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
                            <span className="text-muted-foreground">
                                次 · RIR
                            </span>
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
                            <span className="text-muted-foreground">
                                {ex.progression}
                            </span>
                            {ex.calibration.status === "needs_calibration" && (
                                <Badge
                                    variant="secondary"
                                    className="text-[10px]"
                                >
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
                    具体日程（{scope.start_date} 起至复核日 {scope.review_date}{" "}
                    前）
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

            {cancellations.length > 0 && (
                <div className="rounded-lg border border-destructive/40 bg-destructive/10 p-2.5 text-xs">
                    <p className="font-medium text-destructive">
                        旧版未来未锁定日程取消预览
                    </p>
                    <ul className="mt-1 space-y-0.5 text-muted-foreground">
                        {cancellations.map((s) => (
                            <li key={s.id}>
                                {s.plan_version} {scheduleLine(s)}
                            </li>
                        ))}
                    </ul>
                    <p className="mt-1 text-muted-foreground">
                        已锁定日程不动；取消与新版日程随本草稿确认时同时生效。
                    </p>
                </div>
            )}
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
                <div
                    key={i}
                    className="flex flex-wrap items-center gap-1.5 text-xs"
                >
                    <span className="w-12 text-muted-foreground">
                        第 {i + 1} 组
                    </span>
                    <Badge
                        variant={
                            s.set_type === "warmup" ? "secondary" : "outline"
                        }
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
                        onChange={(e) =>
                            patch(i, { weight_kg: toNumber(e.target.value) })
                        }
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
                        onChange={(e) =>
                            patch(i, { reps: toNumber(e.target.value) })
                        }
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
                        onChange={(e) =>
                            patch(i, { rir: toNumber(e.target.value) })
                        }
                        className="h-7 w-16 text-xs"
                        aria-label={`第 ${i + 1} 组 RIR`}
                    />
                </div>
            ))}
        </div>
    );
}

/* ------------------------------ 档案草稿字段 ------------------------------ */

/** 六类建档事实 + 必填体重的内联纠错（选择 / 数字 / 文本 / 器械与限制列表增删） */
const PROFILE_GOALS = ["增肌（肌肥大）", "力量", "整体健康"];
const PROFILE_EXPERIENCES = ["零基础", "初级（有少量训练经验）", "中级"];
const RESTRICTION_SCOPES: Array<{
    value: Restriction["scope"];
    label: string;
}> = [
    { value: "specific_action", label: "具体动作" },
    { value: "movement_pattern", label: "动作模式" },
];

const selectClass =
    "h-7 rounded-md border border-input bg-transparent px-2 text-xs disabled:opacity-50";

function FieldLine({
    label,
    children,
}: {
    label: string;
    children: ReactNode;
}) {
    return (
        <div className="flex flex-wrap items-center gap-2 text-xs">
            <span className="min-w-24 text-muted-foreground">{label}</span>
            {children}
        </div>
    );
}

function ListField({
    label,
    children,
}: {
    label: string;
    children: ReactNode;
}) {
    return (
        <div className="text-xs">
            <div className="mb-1.5 text-muted-foreground">{label}</div>
            <div className="space-y-1.5">{children}</div>
        </div>
    );
}

/** 选项里补上草稿自带的非表内取值，避免编辑后 select 丢失原值 */
const withCurrent = (options: string[], current?: string): string[] =>
    current !== undefined && !options.includes(current)
        ? [...options, current]
        : options;

/**
 * 档案草稿结构化卡：PRD §5.2 六类事实 + 必填体重，全部只改待确认草稿（不自动提交）。
 * 红旗症状只读展示：红旗由对话报告记入档案，草稿卡不提供解除/编辑（02 2.3：Agent
 * 不诊断、不解除红旗）。
 */
function ProfileDraftFields({
    payload,
    disabled,
    onChange,
}: {
    payload: ProfileDraftPayload;
    disabled: boolean;
    onChange: (payload: ProfileDraftPayload) => void;
}) {
    const [newEquipment, setNewEquipment] = useState("");
    const [newRestriction, setNewRestriction] = useState("");
    const [newRestrictionScope, setNewRestrictionScope] =
        useState<Restriction["scope"]>("specific_action");

    const prof = payload.profile;
    const equipment = prof.equipment;
    const restrictions = payload.restrictions;
    const physical = prof.physical_state;

    const patch = (p: Partial<Profile>) =>
        onChange({ ...payload, profile: { ...prof, ...p } });
    const patchRestriction = (i: number, p: Partial<Restriction>) =>
        onChange({
            ...payload,
            restrictions: (restrictions ?? []).map((r, j) =>
                j === i ? { ...r, ...p } : r,
            ),
        });
    const addRestriction = () => {
        const name = newRestriction.trim();
        if (name === "") return;
        onChange({
            ...payload,
            restrictions: [
                ...(restrictions ?? []),
                { name, scope: newRestrictionScope },
            ],
        });
        setNewRestriction("");
    };

    return (
        <div className="mt-3 space-y-2.5 rounded-lg border border-border bg-muted/30 p-3">
            <p className="text-xs font-medium">
                建档事实（六类 + 体重，均必填；修改后需提交纠错）
            </p>

            <FieldLine label="训练目标">
                <select
                    aria-label="训练目标"
                    className={selectClass}
                    value={prof.goal ?? ""}
                    disabled={disabled}
                    onChange={(e) => patch({ goal: e.target.value })}
                >
                    {prof.goal === undefined && (
                        <option value="">未收集</option>
                    )}
                    {withCurrent(PROFILE_GOALS, prof.goal).map((g) => (
                        <option key={g} value={g}>
                            {g}
                        </option>
                    ))}
                </select>
            </FieldLine>

            <FieldLine label="训练经验">
                <select
                    aria-label="训练经验"
                    className={selectClass}
                    value={prof.experience ?? ""}
                    disabled={disabled}
                    onChange={(e) => patch({ experience: e.target.value })}
                >
                    {prof.experience === undefined && (
                        <option value="">未收集</option>
                    )}
                    {withCurrent(PROFILE_EXPERIENCES, prof.experience).map(
                        (v) => (
                            <option key={v} value={v}>
                                {v}
                            </option>
                        ),
                    )}
                </select>
            </FieldLine>

            <FieldLine label="每周频率">
                <Input
                    type="number"
                    min={1}
                    value={prof.weekly_frequency ?? ""}
                    disabled={disabled}
                    onChange={(e) =>
                        patch({ weekly_frequency: toNumber(e.target.value) })
                    }
                    className="h-7 w-20 text-xs"
                    aria-label="每周训练频率（次）"
                />
                <span className="text-muted-foreground">次 / 周</span>
            </FieldLine>

            <FieldLine label="单次时长">
                <Input
                    type="number"
                    min={1}
                    value={prof.session_minutes ?? ""}
                    disabled={disabled}
                    onChange={(e) =>
                        patch({ session_minutes: toNumber(e.target.value) })
                    }
                    className="h-7 w-20 text-xs"
                    aria-label="单次训练时长（分钟）"
                />
                <span className="text-muted-foreground">分钟</span>
            </FieldLine>

            <FieldLine label="体重（必填）">
                <Input
                    type="number"
                    min={0}
                    step="0.5"
                    value={prof.body_weight_kg ?? ""}
                    disabled={disabled}
                    onChange={(e) =>
                        patch({ body_weight_kg: toNumber(e.target.value) })
                    }
                    className="h-7 w-20 text-xs"
                    aria-label="体重（kg）"
                />
                <span className="text-muted-foreground">kg</span>
            </FieldLine>

            <ListField label="可用器械">
                {equipment === undefined ? (
                    <p className="text-muted-foreground">尚未收集</p>
                ) : (
                    <>
                        {equipment.length === 0 && (
                            <p className="text-muted-foreground">
                                无器械（用户明确说明）
                            </p>
                        )}
                        {equipment.map((name, i) => (
                            <div
                                key={i}
                                className="flex flex-wrap items-center gap-1.5"
                            >
                                <Input
                                    value={name}
                                    disabled={disabled}
                                    onChange={(e) =>
                                        patch({
                                            equipment: equipment.map((n, j) =>
                                                j === i ? e.target.value : n,
                                            ),
                                        })
                                    }
                                    className="h-7 w-40 text-xs"
                                    aria-label={`可用器械 ${i + 1}`}
                                />
                                <Button
                                    variant="ghost"
                                    size="sm"
                                    className="h-7 px-2 text-xs"
                                    disabled={disabled}
                                    onClick={() =>
                                        patch({
                                            equipment: equipment.filter(
                                                (_, j) => j !== i,
                                            ),
                                        })
                                    }
                                    aria-label={`删除器械 ${name === "" ? i + 1 : name}`}
                                >
                                    删除
                                </Button>
                            </div>
                        ))}
                        {!disabled && (
                            <div className="flex flex-wrap items-center gap-1.5">
                                <Input
                                    value={newEquipment}
                                    onChange={(e) =>
                                        setNewEquipment(e.target.value)
                                    }
                                    placeholder="如 杠铃"
                                    className="h-7 w-40 text-xs"
                                    aria-label="新增器械名称"
                                />
                                <Button
                                    variant="outline"
                                    size="sm"
                                    className="h-7 px-2 text-xs"
                                    disabled={newEquipment.trim() === ""}
                                    onClick={() => {
                                        patch({
                                            equipment: [
                                                ...equipment,
                                                newEquipment.trim(),
                                            ],
                                        });
                                        setNewEquipment("");
                                    }}
                                    aria-label="添加器械"
                                >
                                    添加
                                </Button>
                            </div>
                        )}
                    </>
                )}
            </ListField>

            <ListField label="动作限制（当前有效，仅两种粒度）">
                {restrictions === undefined ? (
                    <p className="text-muted-foreground">尚未收集</p>
                ) : (
                    <>
                        {restrictions.length === 0 && (
                            <p className="text-muted-foreground">
                                无（用户明确说明）
                            </p>
                        )}
                        {restrictions.map((r, i) => (
                            <div
                                key={i}
                                className="flex flex-wrap items-center gap-1.5"
                            >
                                <Input
                                    value={r.name}
                                    disabled={disabled}
                                    onChange={(e) =>
                                        patchRestriction(i, {
                                            name: e.target.value,
                                        })
                                    }
                                    className="h-7 w-40 text-xs"
                                    aria-label={`限制 ${i + 1} 名称`}
                                />
                                <select
                                    aria-label={`限制 ${i + 1} 粒度`}
                                    className={selectClass}
                                    value={r.scope}
                                    disabled={disabled}
                                    onChange={(e) =>
                                        patchRestriction(i, {
                                            scope: e.target
                                                .value as Restriction["scope"],
                                        })
                                    }
                                >
                                    {RESTRICTION_SCOPES.map((s) => (
                                        <option key={s.value} value={s.value}>
                                            {s.label}
                                        </option>
                                    ))}
                                </select>
                                {r.note && (
                                    <span
                                        className="max-w-56 truncate text-muted-foreground"
                                        title={r.note}
                                    >
                                        {r.note}
                                    </span>
                                )}
                                <Button
                                    variant="ghost"
                                    size="sm"
                                    className="h-7 px-2 text-xs"
                                    disabled={disabled}
                                    onClick={() =>
                                        onChange({
                                            ...payload,
                                            restrictions: restrictions.filter(
                                                (_, j) => j !== i,
                                            ),
                                        })
                                    }
                                    aria-label={`删除限制 ${r.name === "" ? i + 1 : r.name}`}
                                >
                                    删除
                                </Button>
                            </div>
                        ))}
                        {!disabled && (
                            <div className="flex flex-wrap items-center gap-1.5">
                                <Input
                                    value={newRestriction}
                                    onChange={(e) =>
                                        setNewRestriction(e.target.value)
                                    }
                                    placeholder="如 颈后推举"
                                    className="h-7 w-40 text-xs"
                                    aria-label="新增限制名称"
                                />
                                <select
                                    aria-label="新增限制粒度"
                                    className={selectClass}
                                    value={newRestrictionScope}
                                    onChange={(e) =>
                                        setNewRestrictionScope(
                                            e.target
                                                .value as Restriction["scope"],
                                        )
                                    }
                                >
                                    {RESTRICTION_SCOPES.map((s) => (
                                        <option key={s.value} value={s.value}>
                                            {s.label}
                                        </option>
                                    ))}
                                </select>
                                <Button
                                    variant="outline"
                                    size="sm"
                                    className="h-7 px-2 text-xs"
                                    disabled={newRestriction.trim() === ""}
                                    onClick={addRestriction}
                                    aria-label="添加限制"
                                >
                                    添加
                                </Button>
                            </div>
                        )}
                    </>
                )}
            </ListField>

            <ListField label="当前身体状态与红旗症状（只读；红旗由对话报告记入）">
                <div className="flex flex-wrap items-center gap-1.5">
                    <span className="text-muted-foreground">红旗症状</span>
                    {physical === undefined ? (
                        <span className="text-muted-foreground">尚未收集</span>
                    ) : physical.red_flags.length > 0 ? (
                        physical.red_flags.map((flag) => (
                            <Badge
                                key={flag}
                                variant="destructive"
                                className="text-[10px]"
                            >
                                {flag}
                            </Badge>
                        ))
                    ) : (
                        <span className="text-muted-foreground">
                            无明确红旗（用户确认）
                        </span>
                    )}
                </div>
                <div className="flex flex-wrap items-center gap-1.5">
                    <span className="text-muted-foreground">其他描述</span>
                    {physical === undefined ? (
                        <span className="text-muted-foreground">尚未收集</span>
                    ) : physical.notes.length > 0 ? (
                        <span>{physical.notes.join("、")}</span>
                    ) : (
                        <span className="text-muted-foreground">无</span>
                    )}
                </div>
            </ListField>
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

    // 记录/档案草稿：有未提交纠错时用客户端实时 Diff（展示与 Diff 随纠错更新），
    // 否则展示服务端派生的变更 Diff；计划草稿的 diff 内嵌于 payload（可内联纠错）
    const isRecord = "sets" in payload;
    const isProfile = "profile" in payload;
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
            : "diff" in payload
              ? payload.diff
              : draft.diff;

    return (
        <div className="mt-3 rounded-xl border bg-card p-4 shadow-sm">
            {/* 头部：类型 + 状态 */}
            <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-2 text-sm font-medium">
                    <Sparkles
                        className="size-4 text-muted-foreground"
                        aria-hidden
                    />
                    {KIND_LABEL[draft.kind]}
                    {draft.parent_draft_id !== undefined && (
                        <Badge variant="outline" className="text-[10px]">
                            重算草稿
                        </Badge>
                    )}
                </div>
                <Badge
                    variant={
                        committed
                            ? "default"
                            : discarded
                              ? "outline"
                              : "secondary"
                    }
                    className={discarded ? "text-muted-foreground" : undefined}
                >
                    {committed ? "已采纳" : discarded ? "已丢弃" : "待确认"}
                </Badge>
            </div>

            {/* 内容 + 内联纠错 */}
            {isRecord && (
                <div className="mt-3 space-y-2">
                    <div className="rounded-lg bg-bubble-out px-3 py-2 text-xs text-bubble-out-foreground">
                        <p className="font-medium">+ 新增训练记录</p>
                        <p>
                            {payload.date} · {payload.exercise}（
                            {payload.variant}）
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
                            onChange={(e) =>
                                onChange({ ...payload, date: e.target.value })
                            }
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
            )}
            {/* 档案草稿：结构化事实卡 + 内联纠错（只改待确认草稿，不自动提交） */}
            {isProfile && (
                <ProfileDraftFields
                    payload={payload}
                    disabled={committed || discarded}
                    onChange={onChange}
                />
            )}
            {/* 计划草稿：结构化卡 + 轻量纠错（日期／训练日／动作候选／组数／次数区间／RIR） */}
            {"diff" in payload && (
                <div className="mt-3 space-y-2">
                    <p className="text-sm font-medium">{payload.title}</p>
                    <PlanDraftFields
                        payload={payload}
                        disabled={committed || discarded}
                        onChange={onChange}
                    />
                </div>
            )}

            {/* 变更 Diff（A4） */}
            <div className="mt-3 border-t border-border pt-2">
                <p className="mb-1.5 text-xs font-medium text-muted-foreground">
                    变更 Diff
                    {editedRows.length > 0
                        ? "（已按纠错更新）"
                        : "diff" in payload && dirty
                          ? "（提交纠错后由服务端按修改后内容重算）"
                          : ""}
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
