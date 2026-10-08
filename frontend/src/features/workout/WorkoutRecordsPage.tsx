import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { DatePicker } from "@/components/ui/date-picker";
import { Field, FieldTitle } from "@/components/ui/field";
import { listWorkouts } from "@/lib/api";
import { WORKOUT_QUERY_KEY } from "@/lib/query";
import { cn } from "@/lib/utils";
import WorkoutRecordDetail from "./WorkoutRecordDetail";
import WorkoutRecordList from "./WorkoutRecordList";
import { isRejectedDateRange, resolveSelectedRecord } from "./workoutBrowse";

/** 协议默认每页 10 条，范围 1–100（workout-http-sse-contract §2） */
const PAGE_SIZE = 10;

/** 筛选与分页状态：空日期表示该方向不限日期（§2） */
interface RecordsFilter {
  date_from: string;
  date_to: string;
  page: number;
}

/**
 * 训练记录查询区域（workout-http-sse-contract §2）：只读展示 GET /api/workouts 的完整记录，
 * 按日期范围筛选并分页；查询键含日期范围与分页参数，筛选变化回到第一页，起始晚于结束时就地阻断查询。
 * 栏宽与对话页同栏（``max-w-4xl`` 居中）：容器满 48rem 起列表＋详情双栏（列表 320px，满 56rem 增至 384px），详情常显；
 * 窄处以列表为准、点开详情并可返回，两栏各自滚动。
 * 保存与确认在对话中经自然语言确认后完成，保存落定使本区域查询失效重取。
 */
export default function WorkoutRecordsPage() {
  const [filter, setFilter] = useState<RecordsFilter>({
    date_from: "",
    date_to: "",
    page: 1,
  });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);

  const query = { ...filter, page_size: PAGE_SIZE };
  const blocked = isRejectedDateRange(filter.date_from, filter.date_to);
  const records = useQuery({
    queryKey: [...WORKOUT_QUERY_KEY, "list", query],
    queryFn: ({ signal }) => listWorkouts(query, signal),
    enabled: !blocked,
  });

  const patch = (change: Partial<RecordsFilter>) =>
    setFilter((current) => ({ ...current, ...change }));

  const list = records.data;
  const selected =
    list === undefined ? null : resolveSelectedRecord(list.items, selectedId);

  return (
    <div className="@container mx-auto flex h-full w-full max-w-4xl flex-col overflow-hidden">
      <header className="shrink-0 px-6 pt-8 pb-5">
        <h1 className="font-display text-3xl leading-none font-light tracking-tight">
          训练记录
        </h1>
        <div className="mt-4 flex flex-wrap items-end gap-x-4 gap-y-3">
          <Field className="w-auto gap-1.5">
            <FieldTitle className="text-xs text-muted-foreground">
              起始日期
            </FieldTitle>
            <DatePicker
              label="起始日期"
              emptyText="不限"
              value={filter.date_from}
              className="h-9 w-32 px-3"
              onChange={(date) => patch({ date_from: date, page: 1 })}
            />
          </Field>
          <Field className="w-auto gap-1.5">
            <FieldTitle className="text-xs text-muted-foreground">
              结束日期
            </FieldTitle>
            <DatePicker
              label="结束日期"
              emptyText="不限"
              value={filter.date_to}
              className="h-9 w-32 px-3"
              onChange={(date) => patch({ date_to: date, page: 1 })}
            />
          </Field>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-9 px-3"
            disabled={filter.date_from === "" && filter.date_to === ""}
            onClick={() => patch({ date_from: "", date_to: "", page: 1 })}
          >
            清空筛选
          </Button>
        </div>
        {blocked && (
          <p role="alert" className="mt-3 text-sm text-destructive">
            起始日期晚于结束日期。
          </p>
        )}
      </header>

      {!blocked && (
        <div className="flex min-h-0 flex-1 flex-col @3xl:flex-row">
          <WorkoutRecordList
            list={list}
            error={records.isError ? records.error.message : null}
            fetching={records.isFetching}
            selectedId={selected?.id ?? null}
            className={cn(
              "h-full w-full min-w-0 @3xl:w-80 @3xl:shrink-0 @3xl:border-r @3xl:border-border @4xl:w-96",
              detailOpen ? "hidden @3xl:flex" : "flex",
            )}
            onSelect={(workoutId) => {
              setSelectedId(workoutId);
              setDetailOpen(true);
            }}
            onPageChange={(page) => patch({ page })}
          />
          <WorkoutRecordDetail
            record={selected}
            fetching={records.isFetching}
            className={cn(
              "h-full min-w-0 flex-1",
              detailOpen ? "flex" : "hidden @3xl:flex",
            )}
            onBack={() => setDetailOpen(false)}
          />
        </div>
      )}
    </div>
  );
}
