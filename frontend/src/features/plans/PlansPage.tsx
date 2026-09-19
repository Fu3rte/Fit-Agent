/**
 * /plans 计划页（stage5.md §3.10、Subtask 06；stage6.md §2.5.4）：active、唯一 draft（结构化计划与
 * Evaluator 摘要）与当前计划训练月历的只读展示。
 *
 * 两条硬边界：
 *
 * - **不重算业务数值**：结构化计划与 Evaluator 结果按后端字段原样展示（只做 JSON 缩进），PB／趋势／
 *   完成率等只展示后端文本，不在前端计算、补算或判断进步退步。
 * - **本页没有任何写入入口**：生成／调整计划与 draft 的确认／拒绝都在对话页（stage6.md §2.5.4），
 *   本页不跑 ``/api/agent/run``、不调 ``/api/agent/confirm``／``reject``，也不写任何训练记录。
 */
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { getActivePlan, getCalendarMonth, listPlans } from "@/lib/api";
import type {
  CalendarDayWire,
  CalendarMonthWire,
  CalendarSessionStatusWire,
  CalendarWorkoutWire,
  PlanWire,
} from "@/lib/contract";

/** 计划 Query key：``["plans"]`` 前缀同时覆盖 active 与全部版本列表 */
const ACTIVE_PLAN_KEY = ["plans", "active"];
const PLAN_LIST_KEY = ["plans", "list"];

/** 月历 Query key：对话页的确认／拒绝后用同一 ``calendar`` 前缀失效 */
const calendarKey = (month: string) => ["calendar", month];

const PLAN_STATUS_LABELS: Record<PlanWire["status"], string> = {
  draft: "待确认",
  active: "已启用",
  archived: "已归档",
  rejected: "评估未通过",
};

const SESSION_STATUS_LABELS: Record<CalendarSessionStatusWire, string> = {
  cancelled: "已取消",
  complete: "已完成",
  incomplete: "未完成",
};

const SESSION_STATUS_VARIANTS: Record<
  CalendarSessionStatusWire,
  "default" | "secondary" | "outline"
> = {
  cancelled: "outline",
  complete: "default",
  incomplete: "secondary",
};

/** 月历表头：周一为首列（与 ``monthGrid`` 的前导空格口径一致） */
const WEEKDAY_LABELS = ["一", "二", "三", "四", "五", "六", "日"];

/** 浏览器本地自然月（只作为 ``month=YYYY-MM`` 查询参数的默认值；业务日期一律由服务端判定） */
function currentMonthIso(): string {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
}

/** 月份下拉的固定十二项（严格两位数字，与 ``month=YYYY-MM`` 口径一致） */
const MONTH_OPTIONS = Array.from({ length: 12 }, (_, index) =>
  String(index + 1).padStart(2, "0"),
);

/** 年份下拉的跨度：本地当前年 ±5（只是查询参数的可选区间，不推断计划窗口） */
const YEAR_SPAN = 5;

/**
 * 月历排布：周一为首列的前导空格数与当月天数。
 *
 * 只做网格对齐，不参与任何统计口径；月份取后端回显的严格 ``YYYY-MM``，不由本地输入推断。
 */
function monthGrid(month: string): { leading: number; daysInMonth: number } {
  const [year, monthNumber] = month.split("-").map(Number);
  const leading = (new Date(year, monthNumber - 1, 1).getDay() + 6) % 7;
  return { leading, daysInMonth: new Date(year, monthNumber, 0).getDate() };
}

/** 后端 JSON 字段原样展示：只缩进，不解释领域形状，也不派生任何业务字段 */
function JsonBlock({ value }: { value: unknown }) {
  return (
    <pre className="max-h-72 overflow-auto rounded-md border border-border/60 bg-muted/40 p-3 text-xs leading-relaxed">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

/** 一个计划版本的身份与时间字段；不展示结构化内容（由卡片各自决定） */
function PlanMeta({ plan }: { plan: PlanWire }) {
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
      <Badge variant={plan.status === "active" ? "default" : "outline"}>
        {PLAN_STATUS_LABELS[plan.status]}
      </Badge>
      <span className="tabular-nums">#{plan.id}</span>
      <span className="tabular-nums">版本 {plan.version}</span>
      {plan.source_plan_id !== null && (
        <span className="tabular-nums">来源计划 #{plan.source_plan_id}</span>
      )}
      <span className="tabular-nums">创建 {plan.created_at}</span>
      {plan.confirmed_at !== null && (
        <span className="tabular-nums">启用 {plan.confirmed_at}</span>
      )}
      {plan.archived_at !== null && (
        <span className="tabular-nums">归档 {plan.archived_at}</span>
      )}
    </div>
  );
}

/** 一天的事实：计划日程（含跨日的实际训练日期）与实际训练标记；空白日期不出条目 */
function CalendarDayCell({
  date,
  facts,
}: {
  date: string;
  facts: CalendarDayWire | undefined;
}) {
  return (
    <div className="flex min-h-24 min-w-0 flex-col gap-1 overflow-hidden rounded-md border bg-card p-2">
      <span className="text-xs tabular-nums text-muted-foreground">{date}</span>
      {facts?.plan_sessions.map((session) => {
        const label = `日程 #${session.id} · ${SESSION_STATUS_LABELS[session.status]}`;
        return (
          <div
            key={session.id}
            className="flex min-w-0 flex-col gap-0.5 text-xs"
          >
            <Tooltip>
              <TooltipTrigger asChild>
                <span
                  tabIndex={0}
                  aria-label={label}
                  className="block min-w-0 max-w-full"
                >
                  <Badge
                    variant={SESSION_STATUS_VARIANTS[session.status]}
                    className="max-w-full min-w-0"
                  >
                    <span className="truncate">{label}</span>
                  </Badge>
                </span>
              </TooltipTrigger>
              <TooltipContent>{label}</TooltipContent>
            </Tooltip>
            {session.actual_performed_on !== null &&
              session.actual_performed_on !== date && (
                <span className="text-muted-foreground">
                  实际训练于 {session.actual_performed_on}
                </span>
              )}
          </div>
        );
      })}
      {facts?.workouts.map((workout: CalendarWorkoutWire) => (
        <span key={workout.id} className="text-xs font-medium">
          训练 #{workout.id}
          {workout.plan_session_id === null
            ? " · 额外训练"
            : ` · 完成日程 #${workout.plan_session_id}`}
        </span>
      ))}
    </div>
  );
}

/** 月历：只读取后端返回的事实日期，其余单元格保持空白 */
function CalendarCard({ calendar }: { calendar: CalendarMonthWire }) {
  const grid = useMemo(() => monthGrid(calendar.month), [calendar.month]);
  const factsByDate = useMemo(
    () => new Map(calendar.days.map((day) => [day.date, day])),
    [calendar.days],
  );
  const cells: Array<number | null> = [
    ...Array.from({ length: grid.leading }, () => null),
    ...Array.from({ length: grid.daysInMonth }, (_, index) => index + 1),
  ];

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div className="flex flex-col gap-1.5">
            <CardTitle>训练月历</CardTitle>
            <CardDescription>
              只读取当前 active 计划的日程；没有日程或训练的日期保持空白。
            </CardDescription>
          </div>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        <div className="grid grid-cols-7 gap-2 text-center text-xs text-muted-foreground">
          {WEEKDAY_LABELS.map((label) => (
            <span key={label}>{label}</span>
          ))}
        </div>
        <div className="grid grid-cols-7 gap-2">
          {cells.map((day, index) => {
            if (day === null) {
              /* 网格对齐用的空位：不是日期单元格，因此不出现任何日期或休息日文案 */
              return <div key={`lead-${index}`} aria-hidden />;
            }
            const date = `${calendar.month}-${String(day).padStart(2, "0")}`;
            return (
              <CalendarDayCell
                key={date}
                date={date}
                facts={factsByDate.get(date)}
              />
            );
          })}
        </div>
      </CardContent>
    </Card>
  );
}

/** /plans 计划页：active、待确认 draft 与训练月历的只读展示 */
export default function PlansPage() {
  const [month, setMonth] = useState(currentMonthIso());
  const [year, monthNumber] = month.split("-");
  const yearOptions = Array.from({ length: YEAR_SPAN * 2 + 1 }, (_, index) =>
    String(Number(year) + index - YEAR_SPAN),
  );
  const active = useQuery({ queryKey: ACTIVE_PLAN_KEY, queryFn: getActivePlan });
  const plans = useQuery({ queryKey: PLAN_LIST_KEY, queryFn: listPlans });
  const calendar = useQuery({
    queryKey: calendarKey(month),
    queryFn: () => getCalendarMonth(month),
  });

  const draft =
    plans.data?.plans.find((plan) => plan.status === "draft") ?? null;

  return (
    <div className="mx-auto w-full max-w-3xl px-6 pb-10">
      <header className="pt-10 pb-6">
        <h2 className="font-display text-3xl font-light tracking-tight">
          训练计划
        </h2>
        <p className="mt-1 text-sm text-muted-foreground">
          查看当前启用计划、训练月历与待确认 draft；生成／调整与确认／拒绝都在「对话」页。
        </p>
      </header>

      <div className="flex flex-col gap-6">
        {active.isPending && (
          <p className="text-sm text-muted-foreground">正在加载当前计划…</p>
        )}
        {active.isError && (
          <p className="text-sm text-destructive">
            加载当前计划失败：{active.error.message}，请刷新重试。
          </p>
        )}

        <Card>
          <CardHeader>
            <CardTitle>当前启用计划</CardTitle>
            <CardDescription>
              同一时刻至多一个 active；确认新计划时它会被归档。
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            {active.data ? (
              active.data.plan === null ? (
                <p className="text-sm text-muted-foreground">
                  还没有启用的计划。
                </p>
              ) : (
                <>
                  <PlanMeta plan={active.data.plan} />
                  <JsonBlock value={active.data.plan.structured_content} />
                </>
              )
            ) : (
              <p className="text-xs text-muted-foreground">暂无数据。</p>
            )}
          </CardContent>
        </Card>

        <label className="flex flex-col gap-1 text-sm">
          月份
          <div className="flex items-center gap-2">
            <Select
              value={year}
              onValueChange={(value) => setMonth(`${value}-${monthNumber}`)}
            >
              <SelectTrigger aria-label="年份">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {yearOptions.map((option) => (
                  <SelectItem key={option} value={option}>
                    {option} 年
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select
              value={monthNumber}
              onValueChange={(value) => setMonth(`${year}-${value}`)}
            >
              <SelectTrigger aria-label="月份">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {MONTH_OPTIONS.map((option) => (
                  <SelectItem key={option} value={option}>
                    {Number(option)} 月
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </label>

        {calendar.isError && (
          <p className="text-sm text-destructive">
            月历加载失败：{calendar.error.message}
          </p>
        )}
        {calendar.isPending && (
          <p className="text-sm text-muted-foreground">正在加载月历…</p>
        )}
        {calendar.data && <CalendarCard calendar={calendar.data.calendar} />}

        {plans.isPending && (
          <p className="text-sm text-muted-foreground">正在加载计划版本…</p>
        )}
        {plans.isError && (
          <p className="text-sm text-destructive">
            加载计划版本失败：{plans.error.message}，请刷新重试。
          </p>
        )}

        {draft && (
          <Card>
            <CardHeader>
              <CardTitle>待确认计划</CardTitle>
              <CardDescription>
                确认启用或拒绝归档在「对话」页完成；本页只展示 draft 与 Evaluator 结果。
              </CardDescription>
            </CardHeader>
            <CardContent className="flex flex-col gap-3">
              <PlanMeta plan={draft} />
              <section className="flex flex-col gap-1.5">
                <h4 className="text-sm font-medium">结构化计划</h4>
                <JsonBlock value={draft.structured_content} />
              </section>
              <section className="flex flex-col gap-1.5">
                <h4 className="text-sm font-medium">Evaluator 摘要</h4>
                {draft.evaluator_result === null ? (
                  <p className="text-xs text-muted-foreground">
                    本次 draft 没有评估结果。
                  </p>
                ) : (
                  <JsonBlock value={draft.evaluator_result} />
                )}
              </section>
            </CardContent>
          </Card>
        )}
      </div>
    </div>
  );
}
