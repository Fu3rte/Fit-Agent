import {
  useEffect,
  useMemo,
  useRef,
  type ComponentProps,
  type ComponentType,
} from "react";
import {
  Ban,
  CalendarDays,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Clock,
  Sparkles,
} from "lucide-react";
import type { DayButton } from "react-day-picker";
import { Button } from "@/components/ui/button";
import { Calendar } from "@/components/ui/calendar";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import type {
  CalendarDayWire,
  CalendarMonthWire,
  CalendarSessionStatusWire,
} from "@/lib/contract";
import { cn, dateFromIso, isoFromDate } from "@/lib/utils";

const SESSION_STATUS_LABELS: Record<CalendarSessionStatusWire, string> = {
  cancelled: "已取消",
  complete: "已完成",
  incomplete: "未完成",
};

/* shadcn Calendar 默认取 locale 的窄名（“一”）；周标题与训练月历口径保持“周一” */
const WEEKDAY_FORMAT = new Intl.DateTimeFormat("zh-CN", { weekday: "short" });

type CalendarItemStatus = "completed" | "scheduled" | "extra" | "cancelled";

const CALENDAR_STATUS_STYLE: Record<
  CalendarItemStatus,
  {
    Icon: ComponentType<{ className?: string }>;
    text: string;
    capsule: string;
    dot: string;
  }
> = {
  completed: {
    Icon: CheckCircle2,
    text: "text-emerald-700 dark:text-emerald-400",
    capsule: "border-emerald-500/30 bg-emerald-500/10",
    dot: "bg-emerald-500",
  },
  scheduled: {
    Icon: Clock,
    text: "text-sky-700 dark:text-sky-300",
    capsule: "border-dashed border-sky-500/30 bg-sky-500/10",
    dot: "bg-sky-500",
  },
  extra: {
    Icon: Sparkles,
    text: "text-purple-700 dark:text-purple-300",
    capsule: "border-purple-500/30 bg-purple-500/10",
    dot: "bg-purple-500",
  },
  cancelled: {
    Icon: Ban,
    text: "text-muted-foreground",
    capsule: "border-border bg-muted/40",
    dot: "bg-muted-foreground/50",
  },
};

const CALENDAR_LEGEND: Array<{ status: CalendarItemStatus; label: string }> = [
  { status: "completed", label: SESSION_STATUS_LABELS.complete },
  { status: "scheduled", label: SESSION_STATUS_LABELS.incomplete },
  { status: "cancelled", label: SESSION_STATUS_LABELS.cancelled },
  { status: "extra", label: "额外训练" },
];

interface CalendarItem {
  key: string;
  status: CalendarItemStatus;
  title: string;
  detail: string;
}

/**
 * 当天条目：日程按 ``scheduled_on`` 落天，实际训练按 ``performed_on`` 落天。
 *
 * 当天完成的日程已由对应训练条目承载（后端以 ``actual_performed_on`` 给出同一事实），不重复出条目。
 */
function dayItems(day: CalendarDayWire | undefined): CalendarItem[] {
  if (day === undefined) return [];
  const sessions = day.plan_sessions
    .filter(
      (session) =>
        !(
          session.status === "complete" &&
          session.actual_performed_on === day.date
        ),
    )
    .map<CalendarItem>((session) => ({
      key: `session-${session.id}`,
      status:
        session.status === "complete"
          ? "completed"
          : session.status === "cancelled"
            ? "cancelled"
            : "scheduled",
      title: `日程 #${session.id}`,
      detail:
        session.actual_performed_on === null
          ? SESSION_STATUS_LABELS[session.status]
          : `实际训练于 ${session.actual_performed_on}`,
    }));
  return sessions.concat(
    day.workouts.map((workout) => ({
      key: `workout-${workout.id}`,
      status: workout.plan_session_id === null ? "extra" : "completed",
      title: `训练 #${workout.id}`,
      detail:
        workout.plan_session_id === null
          ? "额外训练"
          : `完成日程 #${workout.plan_session_id}`,
    })),
  );
}

/** DayPicker 的目标月折算成相对后端回显月份的整数偏移，键盘／翻页导航仍走 ``onShiftMonth`` */
function monthDelta(month: string, next: Date): number {
  const [year, monthNumber] = month.split("-").map(Number);
  return (next.getFullYear() - year) * 12 + (next.getMonth() + 1 - monthNumber);
}

/**
 * shadcn Calendar 的日单元内容（``components.DayButton``）：保留日期数字、条目胶囊与 hover 提示，
 * 一天之内仍按后端事实渲染多条胶囊。
 */
function TrainingDayButton({
  day,
  modifiers,
  children,
  items,
  ...props
}: ComponentProps<typeof DayButton> & { items: CalendarItem[] }) {
  const ref = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (modifiers.focused) ref.current?.focus();
  }, [modifiers.focused]);

  const summary =
    items.length === 0
      ? "无安排"
      : items.map((item) => `${item.title} · ${item.detail}`).join("；");

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          ref={ref}
          {...props}
          aria-label={`${props["aria-label"]} ${summary}`}
          className={cn(
            "group flex h-full min-h-24 w-full min-w-0 flex-col gap-1.5 p-1.5 text-left transition-colors @min-[60rem]:min-h-0 focus-visible:z-10 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset focus-visible:outline-none",
            modifiers.selected
              ? "bg-primary/5 ring-2 ring-primary ring-inset"
              : modifiers.outside
                ? "bg-muted/20 hover:bg-muted/40"
                : "bg-card hover:bg-muted/40",
          )}
        >
          <span
            className={cn(
              "inline-flex size-5 shrink-0 items-center justify-center rounded-full text-xs font-medium tabular-nums",
              modifiers.today
                ? "bg-emerald-500 font-bold text-black"
                : modifiers.outside
                  ? "text-muted-foreground/40"
                  : "text-muted-foreground",
            )}
          >
            {children}
          </span>

          <div className="flex min-w-0 flex-1 flex-col gap-1">
            {items.map((item) => {
              const style = CALENDAR_STATUS_STYLE[item.status];
              return (
                <span
                  key={item.key}
                  className={`flex min-w-0 items-center gap-0.5 rounded-md border px-0.5 py-0.5 text-[11px] font-medium ${style.text} ${style.capsule}`}
                >
                  <style.Icon className="size-3 shrink-0" />
                  <span className="truncate">{item.title}</span>
                </span>
              );
            })}

            {!modifiers.outside && items.length === 0 && (
              <span className="flex flex-1 items-center justify-center text-[10px] tracking-widest text-muted-foreground/60 opacity-0 transition-opacity group-hover:opacity-100">
                无安排
              </span>
            )}
          </div>
        </button>
      </TooltipTrigger>
      <TooltipContent>
        {items.length === 0 ? (
          <span className="block text-muted-foreground">无安排</span>
        ) : (
          items.map((item) => (
            <span key={item.key} className="block">
              <span className="font-medium">{item.title}</span>
              <span className="text-muted-foreground"> · {item.detail}</span>
            </span>
          ))
        )}
      </TooltipContent>
    </Tooltip>
  );
}

export default function CalendarCard({
  calendar,
  selectedDate,
  onShiftMonth,
  onSelectDate,
}: {
  calendar: CalendarMonthWire;
  selectedDate: string;
  onShiftMonth: (delta: number) => void;
  onSelectDate: (date: string) => void;
}) {
  const itemsByDate = useMemo(
    () => new Map(calendar.days.map((day) => [day.date, dayItems(day)])),
    [calendar.days],
  );
  /* 日单元按日期取当天条目：组件随条目表一起记忆，避免每次渲染重建组件类型 */
  const components = useMemo(
    () => ({
      DayButton: (props: ComponentProps<typeof DayButton>) => (
        <TrainingDayButton
          {...props}
          items={itemsByDate.get(props.day.isoDate) ?? []}
        />
      ),
    }),
    [itemsByDate],
  );
  const [year, monthNumber] = calendar.month.split("-");

  return (
    <Card className="@min-[60rem]:h-full">
      <CardHeader>
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <CardTitle className="flex shrink-0 items-center gap-2 whitespace-nowrap">
              <CalendarDays className="size-4" />
              训练月历
            </CardTitle>

            <div className="flex shrink-0 items-center gap-0.5 rounded-lg border p-0.5">
              <Button
                variant="ghost"
                size="icon"
                className="size-7"
                aria-label="上一个月"
                onClick={() => onShiftMonth(-1)}
              >
                <ChevronLeft className="size-4" />
              </Button>
              <span className="px-2 font-mono text-xs font-bold tabular-nums">
                {year} 年 {Number(monthNumber)} 月
              </span>
              <Button
                variant="ghost"
                size="icon"
                className="size-7"
                aria-label="下一个月"
                onClick={() => onShiftMonth(1)}
              >
                <ChevronRight className="size-4" />
              </Button>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-3 text-xs">
            {CALENDAR_LEGEND.map((entry) => (
              <span
                key={entry.status}
                className="flex items-center gap-1.5 text-muted-foreground"
              >
                <span
                  className={`size-2 rounded-full ${CALENDAR_STATUS_STYLE[entry.status].dot}`}
                />
                {entry.label}
              </span>
            ))}
            <span className="flex items-center gap-1.5 text-muted-foreground">
              <span className="size-2 rounded-full bg-border" />
              无安排
            </span>
          </div>
        </div>
      </CardHeader>
      <CardContent className="pt-0 @min-[60rem]:min-h-0 @min-[60rem]:flex-1">
        <div className="overflow-hidden rounded-lg border @min-[60rem]:flex @min-[60rem]:h-full @min-[60rem]:flex-col">
          <Calendar
            mode="single"
            required
            hideNavigation
            className="p-0"
            selected={dateFromIso(selectedDate)}
            month={dateFromIso(`${calendar.month}-01`)}
            onMonthChange={(next) =>
              onShiftMonth(monthDelta(calendar.month, next))
            }
            onSelect={(next) => onSelectDate(isoFromDate(next))}
            formatters={{
              formatWeekdayName: (weekday) => WEEKDAY_FORMAT.format(weekday),
            }}
            components={components}
            classNames={{
              root: "flex w-full flex-col @min-[60rem]:min-h-0 @min-[60rem]:flex-1",
              months:
                "flex w-full flex-col @min-[60rem]:min-h-0 @min-[60rem]:flex-1",
              month: "flex w-full flex-1 flex-col",
              month_caption: "hidden",
              month_grid: "flex w-full flex-1 flex-col",
              weekdays:
                "flex w-full border-b bg-muted/30 text-center text-xs text-muted-foreground [&>th:nth-child(6)]:text-emerald-600 [&>th:nth-child(7)]:text-emerald-600 dark:[&>th:nth-child(6)]:text-emerald-400 dark:[&>th:nth-child(7)]:text-emerald-400",
              weekday:
                "min-w-0 flex-1 border-l py-2 font-normal first:border-l-0",
              weeks: "flex flex-1 flex-col",
              week: "flex flex-1 border-b last:border-b-0",
              day: "flex-1 min-w-0 border-l first:border-l-0",
            }}
          />
        </div>
      </CardContent>
    </Card>
  );
}
