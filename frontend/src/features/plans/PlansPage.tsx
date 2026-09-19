import { useState } from "react";
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import ActivePlanCard from "@/features/plans/components/ActivePlanCard";
import CalendarCard from "@/features/plans/components/CalendarCard";
import RecordDayPanel from "@/features/plans/components/RecordDayPanel";
import {
  RecordFormDialog,
  invalidateRecordDerivedQueries,
} from "@/features/plans/components/RecordFormCard";
import {
  deleteRecord,
  getActivePlan,
  getCalendarMonth,
  listExercises,
  listPlans,
  listRecords,
} from "@/lib/api";
import type { PlanWire } from "@/lib/contract";
import { formatTimestamp } from "@/lib/utils";

const ACTIVE_PLAN_KEY = ["plans", "active"];
const PLAN_LIST_KEY = ["plans", "list"];

/** 动作目录 Query key：与计划页、月历明细共用同一份缓存，不另拉一次目录 */
const EXERCISE_LIST_KEY = ["exercises"];

/** 训练记录 Query key：写入后由全量失效刷新（见 ``invalidateRecordDerivedQueries``） */
const RECORD_LIST_KEY = ["records"];

/** 月历 Query key：对话页的确认／拒绝后用同一 ``calendar`` 前缀失效 */
const calendarKey = (month: string) => ["calendar", month];

const PLAN_STATUS_LABELS: Record<PlanWire["status"], string> = {
  draft: "待确认",
  active: "已启用",
  archived: "已归档",
  rejected: "评估未通过",
};

/** 浏览器本地自然月（只作为 ``month=YYYY-MM`` 查询参数的默认值；业务日期一律由服务端判定） */
function currentMonthIso(): string {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
}

/** 浏览器本地自然日（只作为选中日期的默认值；事实日期一律来自服务端回显） */
function currentDateIso(): string {
  return new Date().toLocaleDateString("en-CA");
}

function shiftMonth(month: string, delta: number): string {
  const [year, monthNumber] = month.split("-").map(Number);
  const shifted = new Date(Date.UTC(year, monthNumber - 1 + delta, 1));
  return `${shifted.getUTCFullYear()}-${String(shifted.getUTCMonth() + 1).padStart(2, "0")}`;
}

function JsonBlock({ value }: { value: unknown }) {
  return (
    <pre className="max-h-72 overflow-auto rounded-md border border-border/60 bg-muted/40 p-3 text-xs leading-relaxed">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

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
      <span className="tabular-nums">
        创建 {formatTimestamp(plan.created_at)}
      </span>
      {plan.confirmed_at !== null && (
        <span className="tabular-nums">
          启用 {formatTimestamp(plan.confirmed_at)}
        </span>
      )}
      {plan.archived_at !== null && (
        <span className="tabular-nums">
          归档 {formatTimestamp(plan.archived_at)}
        </span>
      )}
    </div>
  );
}

/**
 * /plans 训练计划：当前启用计划、训练月历与当天记录（新增／编辑／删除）以及待确认 draft。
 *
 * 月历与记录融合在同一屏：左侧月历点选日期，右侧面板给出当天实际训练与未完成的计划日程；
 * 新增与编辑都在弹层里改（窄列里堆叠控件会挤成一团，弹层不占双栏布局）。
 * 分栏按容器可用宽度判定（页面限 max-w-5xl，侧栏折叠也计在内）：宽时左右分栏并占满一屏高度
 * （高 90dvh，日历周行等分拉伸、当天明细过长时面板内部滚动），窄时上下排列按内容自然高度。
 */
export default function PlansPage() {
  const queryClient = useQueryClient();
  const [month, setMonth] = useState(currentMonthIso());
  const [selectedDate, setSelectedDate] = useState(currentDateIso);
  /* null = 表单未展开；"new" = 新增；number = 正在编辑的训练记录 id */
  const [recordForm, setRecordForm] = useState<"new" | number | null>(null);

  const active = useQuery({
    queryKey: ACTIVE_PLAN_KEY,
    queryFn: getActivePlan,
  });
  const plans = useQuery({ queryKey: PLAN_LIST_KEY, queryFn: listPlans });
  const exercises = useQuery({
    queryKey: EXERCISE_LIST_KEY,
    queryFn: listExercises,
  });
  const records = useQuery({
    queryKey: RECORD_LIST_KEY,
    queryFn: listRecords,
  });
  const calendar = useQuery({
    queryKey: calendarKey(month),
    queryFn: () => getCalendarMonth(month),
    /* 切换月份时保留上一月网格，避免每次点击都闪成加载态 */
    placeholderData: keepPreviousData,
  });

  const draft =
    plans.data?.plans.find((plan) => plan.status === "draft") ?? null;

  const catalogue = exercises.data?.exercises ?? [];
  const nameOf = (exerciseId: string) =>
    catalogue.find((exercise) => exercise.id === exerciseId)
      ?.standard_name_zh ?? exerciseId;

  const dayFacts = calendar.data?.calendar.days.find(
    (day) => day.date === selectedDate,
  );
  const dayRecords = (records.data?.records ?? [])
    .filter((record) => record.performed_on === selectedDate)
    .sort((left, right) => left.id - right.id);
  const editingRecord =
    typeof recordForm === "number"
      ? (records.data?.records.find((record) => record.id === recordForm) ??
        null)
      : null;

  const removeRecord = useMutation({
    mutationFn: (recordId: number) => deleteRecord(recordId),
    onSuccess: async () => {
      toast.success("训练记录已删除");
      await invalidateRecordDerivedQueries(queryClient);
    },
    onError: (error) =>
      toast.error(error instanceof Error ? error.message : "训练记录删除失败"),
  });

  /* 点选相邻月份的日期时同步翻月，选中态始终落在当前网格里 */
  const selectDate = (date: string) => {
    setSelectedDate(date);
    setMonth(date.slice(0, 7));
  };

  /* 翻月后选中日期若不在新月份里，落到该月 1 日，避免面板与网格指的不是同一屏 */
  const shiftToMonth = (delta: number) => {
    const next = shiftMonth(month, delta);
    setMonth(next);
    if (!selectedDate.startsWith(next)) setSelectedDate(`${next}-01`);
  };

  return (
    <div className="@container mx-auto w-full max-w-5xl px-6 pt-10 pb-10">
      <div className="flex flex-col gap-6">
        {active.isPending && (
          <p className="text-sm text-muted-foreground">正在加载当前计划…</p>
        )}
        {active.isError && (
          <p className="text-sm text-destructive">
            加载当前计划失败：{active.error.message}，请刷新重试。
          </p>
        )}
        {exercises.isError && (
          <p className="text-sm text-destructive">
            加载动作目录失败：{exercises.error.message}，请刷新重试。
          </p>
        )}

        <ActivePlanCard
          plan={active.data?.plan ?? null}
          exercises={exercises.data?.exercises}
        />

        <section className="grid gap-6 @min-[60rem]:h-[90dvh] @min-[60rem]:grid-cols-12">
          <div className="flex min-w-0 flex-col gap-2 @min-[60rem]:col-span-7 @min-[60rem]:min-h-0">
            {calendar.isError && (
              <p className="text-sm text-destructive">
                月历加载失败：{calendar.error.message}
              </p>
            )}
            {calendar.isPending && (
              <p className="text-sm text-muted-foreground">正在加载月历…</p>
            )}
            {calendar.data && (
              <CalendarCard
                calendar={calendar.data.calendar}
                selectedDate={selectedDate}
                onShiftMonth={shiftToMonth}
                onSelectDate={selectDate}
              />
            )}
          </div>

          <div className="flex min-w-0 flex-col gap-2 @min-[60rem]:col-span-5 @min-[60rem]:min-h-0">
            {records.isError && (
              <p className="text-sm text-destructive">
                加载训练记录失败：{records.error.message}，请刷新重试。
              </p>
            )}
            {records.isPending && (
              <p className="text-sm text-muted-foreground">正在加载训练记录…</p>
            )}
            {records.data && (
              <RecordDayPanel
                date={selectedDate}
                records={dayRecords}
                sessions={dayFacts?.plan_sessions ?? []}
                nameOf={nameOf}
                onCreate={() => setRecordForm("new")}
                onEdit={(recordId) => setRecordForm(recordId)}
                onDelete={async (recordId) => {
                  await removeRecord.mutateAsync(recordId);
                }}
              />
            )}
          </div>
        </section>

        {/* 新增与编辑都在弹层里改，双栏高度不被表单擑开 */}
        {recordForm !== null && (
          <RecordFormDialog
            key={recordForm}
            record={editingRecord}
            initialDate={selectedDate}
            exercises={catalogue}
            onDone={() => setRecordForm(null)}
          />
        )}

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
                确认启用或拒绝归档在「对话」页完成；本页只展示 draft 与
                Evaluator 结果。
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
