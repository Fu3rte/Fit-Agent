import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowRight,
  MessagesSquare,
  ShieldAlert,
  ShieldCheck,
} from "lucide-react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { buttonVariants } from "@/components/ui/button";
import { getArrangements, getProfile } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { ReactNode } from "react";
import type {
  AcceptedArrangement,
  NeedsCalibration,
  PlanPayload,
  PlanSafetyReview,
  PlanScheduleEntry,
  PlanVersion,
  Profile,
  Restriction,
} from "@/lib/contract";
import {
  arrangementStatusLabel,
  classifyArrangementStatus,
  derivePlanBlocks,
  effortPlainLabel,
  findWorkout,
  prescriptionLabel,
  progressionLabel,
  weekdayLabel,
  type DisplayBlock,
} from "@/lib/planView";

function Loading({ text }: { text: string }) {
  return <p className="mt-10 text-sm text-muted-foreground">{text}…</p>;
}

function LoadError({ text }: { text: string }) {
  return (
    <p className="mt-10 text-sm text-destructive">
      加载失败：{text}，请刷新重试。
    </p>
  );
}

interface ProfileRow {
  label: string;
  value: ReactNode;
}

/** 档案卡：八项事实（目标/经验/频率/时长/器械/体重/身体情况，PRD 5.2，只读） */
function ProfileCard({ profile }: { profile: Profile }) {
  const rows: ProfileRow[] = [
    { label: "训练目标", value: profile.goal },
    { label: "训练经验", value: profile.experience },
    { label: "每周频率", value: `${profile.weekly_frequency} 次 / 周` },
    { label: "单次时长", value: `${profile.session_minutes} 分钟` },
    {
      label: "可用器械",
      value: profile.equipment.length > 0 ? profile.equipment.join("、") : "无",
    },
    { label: "体重", value: `${profile.body_weight_kg} kg` },
    {
      label: "身体情况",
      value:
        profile.body_conditions.length > 0 ? (
          <span className="flex flex-col items-end gap-0.5">
            {profile.body_conditions.map((c) => (
              <span key={c}>{c}</span>
            ))}
          </span>
        ) : (
          "无（用户确认）"
        ),
    },
  ];
  return (
    <Card>
      <CardHeader>
        <CardTitle>档案</CardTitle>
        <CardDescription>已确认的训练档案事实</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2.5 text-sm">
        {rows.map((row) => (
          <div
            key={row.label}
            className="flex items-baseline justify-between gap-4"
          >
            <span className="shrink-0 text-muted-foreground">{row.label}</span>
            <span className="text-right font-medium">{row.value}</span>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

/** 动作限制卡 */
function RestrictionsCard({ restrictions }: { restrictions: Restriction[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>动作限制</CardTitle>
        <CardDescription>
          红色「暂禁」仅为展示文案；限制经确认长期保留，增删须在对话页经草稿确认
        </CardDescription>
      </CardHeader>
      <CardContent>
        {restrictions.length === 0 ? (
          <p className="text-sm text-muted-foreground">暂无动作限制</p>
        ) : (
          <ul className="flex flex-wrap gap-2">
            {restrictions.map((r) => (
              <li key={r.name}>
                <Badge
                  variant="destructive"
                  title={r.note}
                  className="gap-1 py-1"
                >
                  <ShieldAlert className="size-3" aria-hidden />
                  {r.name}
                  {" · "}
                  {r.scope === "specific_action" ? "具体动作" : "动作模式"}
                  {" · 暂禁"}
                </Badge>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

/** 单个训练日的动作表（展示派生 DisplayBlock） */
function BlockTable({ block }: { block: DisplayBlock }) {
  return (
    <div>
      <div className="mb-2 flex items-baseline justify-between">
        <h4 className="text-sm font-medium">{block.name}</h4>
        <span className="text-xs text-muted-foreground">
          {block.weekday !== undefined
            ? `每周${weekdayLabel(block.weekday)}`
            : "未排入循环"}
        </span>
      </div>
      <table className="w-full table-fixed text-sm">
        <thead>
          <tr className="border-b text-left text-xs text-muted-foreground">
            <th className="w-[36%] py-1.5 pr-3 font-normal">动作</th>
            <th className="w-[18%] py-1.5 pr-3 font-normal">组 × 次</th>
            <th className="w-[20%] py-1.5 pr-3 font-normal">目标用力</th>
            <th className="w-[26%] py-1.5 font-normal">渐进方式</th>
          </tr>
        </thead>
        <tbody>
          {block.exercises.map((ex) => {
            const effort =
              ex.prescription.kind === "reps" && ex.prescription.target_rir
                ? effortPlainLabel(ex.prescription.target_rir)
                : "—";
            return (
              <tr
                key={ex.item_key}
                className="border-b last:border-0"
              >
                <td className="py-2 pr-3">
                  {ex.display_snapshot.name}
                  <span className="ml-1.5 text-xs text-muted-foreground">
                    {ex.display_snapshot.equipment_variant}
                  </span>
                </td>
                <td className="py-2 pr-3 tabular-nums">
                  {prescriptionLabel(ex.prescription)}
                </td>
                <td className="py-2 pr-3 text-xs">{effort}</td>
                <td className="py-2 text-xs text-muted-foreground">
                  {progressionLabel(ex.progression.method)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** 使用前安全复核结果（04 4.5 整份复核） */
function PlanSafetyNotice({ safety }: { safety: PlanSafetyReview }) {
  if (safety.usable)
    return (
      <p className="flex items-start gap-1.5 rounded-lg border bg-muted/40 p-3 text-sm text-muted-foreground">
        <ShieldCheck className="mt-0.5 size-4 shrink-0" aria-hidden />
        <span>
          已按最新身体情况与限制复核整份计划（业务版本 {safety.context_version}
          ）：可给出基于该计划的训练指导。
        </span>
      </p>
    );
  return (
    <div className="rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-sm">
      <p className="flex items-center gap-1.5 font-medium text-destructive">
        <ShieldAlert className="size-4 shrink-0" aria-hidden />
        整份计划指导已阻断
      </p>
      {safety.block_code === "plan_action_unavailable" && (
        <p className="mt-1.5">
          计划引用的动作目录身份缺失（plan_action_unavailable）：无法安全给出基于该计划的处方。
        </p>
      )}
      {safety.red_flag_blocked && (
        <p className="mt-1.5">
          档案身体情况命中需线下评估的安全性症状：不给任何基于该计划的处方，请先完成线下专业评估。
        </p>
      )}
      {safety.conflicts.length > 0 && (
        <>
          <p className="mt-1.5">
            以下动作命中当前有效限制；任一冲突即整份阻断，不会只跳过冲突动作、继续给其余动作的处方。
          </p>
          <ul className="mt-1.5 flex list-disc flex-col gap-0.5 pl-5">
            {safety.conflicts.map((c) => (
              <li key={`${c.exercise_id}·${c.restriction.name}`}>
                {c.exercise_name} · 命中限制「{c.restriction.name}」（
                {c.restriction.scope === "specific_action"
                  ? "具体动作"
                  : "动作模式"}
                ）
              </li>
            ))}
          </ul>
        </>
      )}
      <p className="mt-1.5 text-muted-foreground">
        当前计划内容仍按原样展示；修改请从对话发起修订草稿，确认后生成新版本。
      </p>
    </div>
  );
}

/** 校准说明（D3）：pass = 稳定完成处方次数下限；RIR 不作通过硬性条件 */
function CalibrationSection({ calibration }: { calibration: NeedsCalibration }) {
  return (
    <section>
      <div className="mb-2 flex items-center gap-2">
        <h4 className="text-sm font-medium">负荷校准</h4>
        <Badge variant="secondary" className="py-0.5">
          需要校准
        </Badge>
      </div>
      <p className="text-xs text-muted-foreground">
        无可信训练记录：不给具体起始重量，也不按体重、估算 1RM 或默认杠重猜测。
      </p>
      <ol className="mt-2 flex list-decimal flex-col gap-1 pl-5 text-sm">
        {calibration.steps.map((step) => (
          <li key={step}>{step}</li>
        ))}
      </ol>
      <p className="mt-2 text-sm">
        <span className="text-muted-foreground">通过标准：</span>
        {calibration.pass_criteria}
      </p>
      <p className="mt-2 text-sm">
        <span className="text-muted-foreground">停止条件：</span>
        {calibration.stop_criteria}
      </p>
    </section>
  );
}

/**
 * 日程行徽章（锁定双态 + 安排状态联表，F3-03）：
 * - 锁定优先展示「已锁定（日期规则）」；stored cancelled 展示「已取消」；
 * - 安排状态：尚无安排 / 已接受安排 · 已调整|目标更保守|未调整 + 接受时间。
 */
function ScheduleBadge({
  entry,
  plan,
  arrangement,
}: {
  entry: PlanScheduleEntry;
  plan: PlanPayload;
  arrangement?: AcceptedArrangement;
}) {
  const cancelled = entry.stored_status === "cancelled";
  const locked = entry.locked_effective && !cancelled;
  const lockLabel = cancelled
    ? "已取消"
    : locked
      ? entry.locked_by_date_rule
        ? "已锁定（日期规则）"
        : "已锁定"
      : "应训练";
  const status = arrangement
    ? classifyArrangementStatus(
        arrangement,
        findWorkout(plan, entry.plan_workout_key)?.exercises ?? [],
      )
    : null;
  const acceptedAt = arrangement
    ? `${arrangement.accepted_at.slice(0, 10)} ${arrangement.accepted_at.slice(11, 16)}`
    : "";
  return (
    <>
      <Badge
        variant={cancelled ? "secondary" : locked ? "outline" : "default"}
        className={cn("py-1", cancelled && "line-through opacity-70")}
      >
        {lockLabel}
      </Badge>
      <Badge
        variant={status ? "secondary" : "outline"}
        className="py-1"
        title={status ? acceptedAt : undefined}
      >
        {status
          ? `${arrangementStatusLabel(status)} · ${acceptedAt}`
          : "尚无安排"}
      </Badge>
    </>
  );
}

/** 具体日程（04 4.2/4.4）：当前版本条目；锁定用 locked_effective；安排只读联表 */
function ScheduleSection({
  entries,
  plan,
  arrangements,
}: {
  entries: PlanScheduleEntry[];
  plan: PlanPayload;
  arrangements: AcceptedArrangement[];
}) {
  const bySession = new Map(
    arrangements.map((a) => [a.target.scheduled_session_id, a]),
  );
  const byDate = new Map(arrangements.map((a) => [a.target.scheduled_on, a]));
  return (
    <section>
      <h4 className="text-sm font-medium">具体日程</h4>
      <p className="mt-1 text-xs text-muted-foreground">
        仅列当前版本的应训练日（休息日不排）；到期即锁（存储标记 ∪
        日期规则）；安排状态联表自已接受安排。
      </p>
      <ul className="mt-2 flex flex-wrap gap-1.5">
        {entries.map((s) => (
          <li
            key={s.id}
            className="inline-flex items-center gap-1.5 rounded-full border py-0.5 pr-1 pl-2 text-xs"
          >
            <span className="tabular-nums">
              {s.date.slice(5)} {weekdayLabel(s.weekday)}
            </span>
            <ScheduleBadge
              entry={s}
              plan={plan}
              arrangement={bySession.get(s.id) ?? byDate.get(s.date)}
            />
          </li>
        ))}
      </ul>
    </section>
  );
}

/**
 * 当前计划卡：状态／版本／日期、安全复核结果、按训练日分组的处方（展示经 derive）、
 * 校准说明与具体日程（effective 锁定）。
 */
function PlanCard({
  plan,
  schedules,
  safety,
  arrangements,
}: {
  plan: PlanVersion;
  schedules: PlanScheduleEntry[];
  safety?: PlanSafetyReview;
  arrangements: AcceptedArrangement[];
}) {
  const blocks = derivePlanBlocks(plan.payload);
  const calibration = plan.payload.plan_workouts
    .flatMap((w) => w.exercises)
    .find((e) => e.load?.kind === "needs_calibration")?.load;
  const currentSchedules = schedules.filter(
    (s) => s.plan_version === plan.version && s.stored_status !== "cancelled",
  );
  return (
    <Card className="sm:col-span-2">
      <CardHeader>
        <div className="flex flex-wrap items-center gap-2">
          <CardTitle>当前计划</CardTitle>
          <Badge variant={plan.status === "active" ? "default" : "secondary"}>
            {plan.status === "active" ? "生效中" : "已归档"}
          </Badge>
        </div>
        <CardDescription>
          {plan.version} · 开始 {plan.starts_on} · 复核 {plan.review_on}
          ；处方修改须经对话草稿确认并生成新版本
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-6">
        {safety && <PlanSafetyNotice safety={safety} />}
        {blocks.map((block) => (
          <BlockTable key={block.workout_key} block={block} />
        ))}
        {calibration && calibration.kind === "needs_calibration" && (
          <CalibrationSection calibration={calibration} />
        )}
        {currentSchedules.length > 0 && (
          <ScheduleSection
            entries={currentSchedules}
            plan={plan.payload}
            arrangements={arrangements}
          />
        )}
      </CardContent>
    </Card>
  );
}

/** /profile 档案与限制：只读看板，业务变更唯一入口是对话 */
export default function ProfilePage() {
  const profile = useQuery({ queryKey: ["profile"], queryFn: getProfile });
  const arrangements = useQuery({
    queryKey: ["arrangements"],
    queryFn: getArrangements,
  });

  return (
    <div className="mx-auto w-full max-w-3xl px-6 pb-10">
      <header className="pt-10 pb-6">
        <h2 className="font-display text-3xl font-light tracking-tight">
          档案与限制
        </h2>
        <p className="mt-1 text-sm text-muted-foreground">
          档案、动作限制与当前计划的只读查看
        </p>
      </header>

      {profile.isPending && <Loading text="正在加载档案" />}
      {profile.isError && <LoadError text={profile.error.message} />}

      {profile.data && profile.data.profile === null && (
        <Card className="mt-6">
          <CardHeader>
            <div className="flex items-center gap-2">
              <MessagesSquare
                className="size-5 shrink-0 text-muted-foreground"
                aria-hidden
              />
              <CardTitle>尚未建档</CardTitle>
            </div>
            <CardDescription>
              建档只能通过对话完成，没有独立表单；信息齐备后生成档案草稿，确认后在此查看档案与限制。
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Link to="/" className={buttonVariants({ size: "sm" })}>
              前往对话开始建档
              <ArrowRight aria-hidden />
            </Link>
          </CardContent>
        </Card>
      )}

      {profile.data && profile.data.profile && (
        <>
          <div className="grid gap-4 sm:grid-cols-2">
            <ProfileCard profile={profile.data.profile} />
            <RestrictionsCard restrictions={profile.data.restrictions} />
            {profile.data.plan ? (
              <PlanCard
                plan={profile.data.plan}
                schedules={profile.data.schedules ?? []}
                safety={profile.data.plan_safety}
                arrangements={arrangements.data?.arrangements ?? []}
              />
            ) : (
              <Card className="sm:col-span-2">
                <CardHeader>
                  <CardTitle>当前计划</CardTitle>
                  <CardDescription>
                    尚无计划；计划只能从对话生成，确认启用后才在此展示处方与日程
                  </CardDescription>
                </CardHeader>
                <CardContent>
                  <p className="text-sm text-muted-foreground">尚无计划</p>
                </CardContent>
              </Card>
            )}
          </div>

          <footer className="mt-8 flex items-center justify-center gap-1.5 text-sm text-muted-foreground">
            变更请到对话页发起
            <Link
              to="/"
              className="inline-flex items-center gap-1 font-medium text-foreground underline-offset-4 hover:underline"
            >
              前往对话
              <ArrowRight className="size-3.5" aria-hidden />
            </Link>
          </footer>
        </>
      )}
    </div>
  );
}
