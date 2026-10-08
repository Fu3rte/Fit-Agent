import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { DatePicker } from "@/components/ui/date-picker";
import { Field, FieldTitle } from "@/components/ui/field";
import {
  Pagination,
  PaginationContent,
  PaginationItem,
} from "@/components/ui/pagination";
import { listWorkouts } from "@/lib/api";
import { WORKOUT_QUERY_KEY } from "@/lib/query";
import WorkoutFields from "./WorkoutFields";

/** 协议默认每页 10 条，范围 1–100（workout-http-sse-contract §2） */
const PAGE_SIZE = 10;

/** 筛选与分页状态：空日期表示该方向不限日期（§2） */
interface RecordsFilter {
  date_from: string;
  date_to: string;
  page: number;
}

/**
 * 训练记录查询区域（workout-http-sse-contract §2、PRODUCT.md §3.3）：只读展示 GET /api/workouts 的完整记录，
 * 按日期范围筛选并分页；查询键含日期范围与分页参数，筛选变化回到第一页。
 * 保存与确认在对话中经自然语言确认后完成，保存落定使本区域查询失效重取。
 */
export default function WorkoutRecordsPage() {
  const [filter, setFilter] = useState<RecordsFilter>({
    date_from: "",
    date_to: "",
    page: 1,
  });
  const query = { ...filter, page_size: PAGE_SIZE };
  const records = useQuery({
    queryKey: [...WORKOUT_QUERY_KEY, "list", query],
    queryFn: ({ signal }) => listWorkouts(query, signal),
  });

  const patch = (change: Partial<RecordsFilter>) =>
    setFilter((current) => ({ ...current, ...change }));

  const list = records.data;
  const totalPages =
    list === undefined ? 1 : Math.max(1, Math.ceil(list.total / PAGE_SIZE));
  const fetching = records.isFetching;

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex w-full max-w-4xl flex-col gap-6 px-6 py-10">
        <Card>
          <CardHeader>
            <CardTitle>训练记录</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="grid gap-4 sm:grid-cols-2">
              {(
                [
                  ["date_from", "起始日期"],
                  ["date_to", "结束日期"],
                ] as const
              ).map(([key, title]) => (
                <Field key={key} className="gap-2">
                  <FieldTitle>{title}</FieldTitle>
                  <DatePicker
                    label={title}
                    emptyText="不限"
                    value={filter[key]}
                    onChange={(date) => patch({ [key]: date, page: 1 })}
                  />
                </Field>
              ))}
            </div>
          </CardContent>
        </Card>

        {records.isError && (
          <p role="alert" className="text-sm text-destructive">
            {records.error.message}
          </p>
        )}

        <div className="flex flex-col gap-4" aria-busy={fetching}>
          {list !== undefined && list.items.length === 0 && (
            <p role="status" className="text-sm">
              没有符合条件的训练记录。
            </p>
          )}
          {list?.items.map((record) => (
            <Card key={record.id}>
              <CardHeader>
                <CardTitle className="tabular-nums">
                  {record.performed_on}
                </CardTitle>
              </CardHeader>
              <CardContent>
                <WorkoutFields content={record.content} />
              </CardContent>
            </Card>
          ))}
        </div>

        {list !== undefined && (
          <div className="flex flex-col gap-2">
            <p className="text-center text-sm tabular-nums">
              第 {list.page} / {totalPages} 页 · 共 {list.total} 条
            </p>
            <Pagination>
              <PaginationContent>
                <PaginationItem>
                  <Button
                    type="button"
                    variant="outline"
                    size="icon"
                    aria-label="上一页"
                    disabled={fetching || list.page <= 1}
                    onClick={() => patch({ page: list.page - 1 })}
                  >
                    <ChevronLeft aria-hidden />
                  </Button>
                </PaginationItem>
                <PaginationItem>
                  <Button
                    type="button"
                    variant="outline"
                    size="icon"
                    aria-label="下一页"
                    disabled={fetching || list.page >= totalPages}
                    onClick={() => patch({ page: list.page + 1 })}
                  >
                    <ChevronRight aria-hidden />
                  </Button>
                </PaginationItem>
              </PaginationContent>
            </Pagination>
          </div>
        )}
      </div>
    </div>
  );
}
