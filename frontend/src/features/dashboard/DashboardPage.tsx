/**
 * /dashboard 数据看板（stage2.md §10、Subtask 05）：月历事实、最近 30 天体重／体脂折线图与三类 PB。
 *
 * 三条硬边界：
 *
 * - **只展示后端事实**：PB、趋势和日程状态都由 ``/api/stats/*`` 现算返回，本页不重算、不缓存派生值。
 * - **不展示力量趋势**：``trends.strength`` 由后端按截至各日期的累计 PB 计算并暴露，Stage 2 看板
 *   不渲染该曲线，也不提供动作选择 UI。
 * - **不补 0**：窗口内只在真实存在记录的日期出点，无数据／数据不足显示空态文案；不展示训练容量、
 *   估算 1RM、完成率或效果评价，也不为空白日期造「休息日」占位。
 *
 * 缓存失效：训练与身体数据写入在记录页用 ``queryClient.invalidateQueries()`` 全量失效，
 * 本页三个 Query（personal-bests／trends／calendar）随刷新自动重取。
 */
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip as ChartTooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { getCalendarMonth, getTrends, listPersonalBests } from "@/lib/api";
import type {
  CalendarDayWire,
  CalendarMonthWire,
  CalendarSessionStatusWire,
  CalendarWorkoutWire,
  MetricChangeWire,
  MetricPointWire,
  PersonalBestTypeWire,
  PersonalBestWire,
  TrendStatusWire,
  WorkoutGapWire,
} from "@/lib/contract";

/** 看板 Query key：记录页用全量 ``invalidateQueries()``，三个 key 都会随写入刷新 */
const PERSONAL_BESTS_KEY = ["personal-bests"];
const TRENDS_KEY = ["trends"];
const calendarKey = (month: string) => ["calendar", month];

const PB_TYPE_LABELS: Record<PersonalBestTypeWire, string> = {
  weight_pb: "最大重量",
  reps_pb: "最大次数",
  duration_pb: "最长时长",
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

function pbValueText(pb: PersonalBestWire): string {
  if (pb.pb_type === "weight_pb") return `${pb.value} kg`;
  if (pb.pb_type === "reps_pb") return `${pb.value} 次`;
  return `${pb.value} 秒`;
}

/** 后端已给出状态与取值；这里只把它写成一句话，不判断进步、退步或停滞 */
function changeText(change: MetricChangeWire, unit: string): string {
  if (change.status === "no_data") return "暂无记录";
  if (change.status === "insufficient_data")
    return "仅有 1 条记录，暂不给出变化";
  const delta =
    change.change === null
      ? ""
      : `${change.change > 0 ? "+" : ""}${change.change}`;
  return `最近 ${change.current_on} ${change.current}${unit}，上一条 ${change.previous_on} ${change.previous}${unit}，变化 ${delta}${unit}`;
}

function gapText(gap: WorkoutGapWire): string {
  if (gap.status === "no_data") return "暂无训练记录";
  return `距上次训练 ${gap.days} 天（${gap.last_performed_on}）`;
}

function statusHint(status: TrendStatusWire): string {
  return status === "ok" ? "" : "（数据不足）";
}

/** 一张体重或体脂折线图：窗口与点都由后端给出，无点即空态，不补 0 */
function MetricChartCard({
  title,
  unit,
  points,
  change,
}: {
  title: string;
  unit: string;
  points: MetricPointWire[];
  change: MetricChangeWire;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        <CardDescription>
          最近 30 天；只在有记录的日期出点{statusHint(change.status)}。
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {points.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            暂无记录，暂不绘制折线。
          </p>
        ) : (
          <div className="h-56 w-full">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart
                data={points}
                margin={{ top: 8, right: 16, bottom: 0, left: -16 }}
              >
                <CartesianGrid
                  stroke="var(--border)"
                  strokeDasharray="3 3"
                  vertical={false}
                />
                <XAxis
                  dataKey="measured_on"
                  tick={{ fontSize: 11 }}
                  stroke="var(--muted-foreground)"
                />
                <YAxis
                  domain={["auto", "auto"]}
                  tick={{ fontSize: 11 }}
                  stroke="var(--muted-foreground)"
                  unit={unit}
                  width={56}
                />
                <ChartTooltip
                  contentStyle={{
                    background: "var(--card)",
                    border: "1px solid var(--border)",
                    borderRadius: "0.5rem",
                    fontSize: 12,
                    color: "var(--card-foreground)",
                  }}
                />
                <Line
                  type="monotone"
                  dataKey="value"
                  stroke="var(--primary)"
                  strokeWidth={2}
                  dot={{ r: 3, fill: "var(--primary)" }}
                  isAnimationActive={false}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
        )}
        <p className="text-xs text-muted-foreground">
          {changeText(change, unit)}
        </p>
      </CardContent>
    </Card>
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

/** 三类 PB：数值、适用重量与来源训练／组序号／日期，全部来自后端现算结果 */
function PersonalBestCard({ bests }: { bests: PersonalBestWire[] }) {
  const byType: Record<PersonalBestTypeWire, PersonalBestWire[]> = {
    weight_pb: [],
    reps_pb: [],
    duration_pb: [],
  };
  for (const pb of bests) byType[pb.pb_type].push(pb);

  return (
    <Card>
      <CardHeader>
        <CardTitle>个人最好成绩（PB）</CardTitle>
        <CardDescription>
          三类 PB 由后端按当前有效工作组现算；修改或删除记录后立即重算。
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {(Object.keys(byType) as PersonalBestTypeWire[]).map((pbType) => (
          <section key={pbType} className="flex flex-col gap-2">
            <h4 className="text-sm font-medium">{PB_TYPE_LABELS[pbType]}</h4>
            {byType[pbType].length === 0 ? (
              <p className="text-xs text-muted-foreground">暂无记录。</p>
            ) : (
              <ul className="flex flex-col gap-1 text-sm">
                {byType[pbType].map((pb) => (
                  <li
                    key={`${pb.exercise_id}-${pb.pb_type}-${pb.weight_kg ?? "none"}`}
                    className="flex flex-wrap items-baseline gap-2"
                  >
                    <span>{pb.exercise_name}</span>
                    <span className="tabular-nums">{pbValueText(pb)}</span>
                    <span className="text-xs text-muted-foreground">
                      来源 {pb.performed_on} · 训练 #{pb.workout_session_id} 第{" "}
                      {pb.set_no} 组
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>
        ))}
        {bests.length === 0 && (
          <p className="text-xs text-muted-foreground">
            还没有可计入 PB 的有效工作组。
          </p>
        )}
      </CardContent>
    </Card>
  );
}

/** /dashboard 数据看板：月历 + 最近 30 天体重／体脂折线 + 三类 PB */
export default function DashboardPage() {
  const [month, setMonth] = useState(currentMonthIso());
  const bests = useQuery({
    queryKey: PERSONAL_BESTS_KEY,
    queryFn: listPersonalBests,
  });
  const trends = useQuery({ queryKey: TRENDS_KEY, queryFn: getTrends });
  const calendar = useQuery({
    queryKey: calendarKey(month),
    queryFn: () => getCalendarMonth(month),
    enabled: month !== "",
  });

  const trendSummary = trends.data?.trends.trend_summary;

  return (
    <div className="mx-auto w-full max-w-5xl px-6 pb-10">
      <header className="pt-10 pb-6">
        <h2 className="font-display text-3xl font-light tracking-tight">
          数据看板
        </h2>
        <p className="mt-1 text-sm text-muted-foreground">
          日历、最近 30 天体重与体脂趋势、三类 PB；全部由后端按有效记录现算。
        </p>
        {trendSummary && (
          <p className="mt-1 text-xs text-muted-foreground">
            {gapText(trendSummary.days_since_last_workout)}
          </p>
        )}
      </header>

      <div className="flex flex-col gap-6">
        {bests.isError && (
          <p className="text-sm text-destructive">
            PB 加载失败：{bests.error.message}
          </p>
        )}
        {bests.isPending && (
          <p className="text-sm text-muted-foreground">正在加载 PB…</p>
        )}
        {bests.data && <PersonalBestCard bests={bests.data.personal_bests} />}

        <label className="flex w-40 flex-col gap-1 text-sm">
          月份
          <Input
            type="month"
            value={month}
            onChange={(event) => setMonth(event.target.value)}
          />
        </label>
        {month === "" ? (
          <p className="text-sm text-muted-foreground">请选择月份。</p>
        ) : (
          <>
            {calendar.isError && (
              <p className="text-sm text-destructive">
                月历加载失败：{calendar.error.message}
              </p>
            )}
            {calendar.isPending && (
              <p className="text-sm text-muted-foreground">正在加载月历…</p>
            )}
            {calendar.data && (
              <CalendarCard calendar={calendar.data.calendar} />
            )}
          </>
        )}

        {trends.isError && (
          <p className="text-sm text-destructive">
            趋势加载失败：{trends.error.message}
          </p>
        )}
        {trends.isPending && (
          <p className="text-sm text-muted-foreground">正在加载趋势…</p>
        )}
        {trends.data && (
          <>
            <p className="text-xs text-muted-foreground">
              趋势窗口 {trends.data.trends.from} 至 {trends.data.trends.to}（
              {trends.data.trends.window_days} 天）。
            </p>
            <MetricChartCard
              title="体重趋势"
              unit="kg"
              points={trends.data.trends.weight}
              change={trends.data.trends.trend_summary.weight_change}
            />
            <MetricChartCard
              title="体脂趋势"
              unit="%"
              points={trends.data.trends.body_fat}
              change={trends.data.trends.trend_summary.body_fat_change}
            />
          </>
        )}
      </div>
    </div>
  );
}
