import { ChevronLeft, ChevronRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Pagination,
  PaginationContent,
  PaginationItem,
} from "@/components/ui/pagination";
import { cn } from "@/lib/utils";
import type { WorkoutListWire } from "@/lib/contract";
import { describeWorkoutContent } from "./workoutBrowse";

/** 初次加载骨架：与列表项同结构，避免布局在数据到达后跳动 */
function ListSkeleton() {
  return (
    <ul className="flex flex-col gap-2" aria-hidden>
      {[0, 1, 2, 3, 4].map((row) => (
        <li
          key={row}
          className="animate-pulse rounded-lg border border-border bg-card p-4"
        >
          <span className="block h-5 w-28 rounded bg-muted" />
          <span className="mt-2 block h-3 w-36 rounded bg-muted" />
          <span className="mt-3 flex gap-1.5">
            <span className="h-5 w-20 rounded-full bg-muted" />
            <span className="h-5 w-14 rounded-full bg-muted" />
          </span>
        </li>
      ))}
    </ul>
  );
}

interface WorkoutRecordListProps {
  list: WorkoutListWire | undefined;
  error: string | null;
  fetching: boolean;
  selectedId: string | null;
  className?: string;
  onSelect: (workoutId: string) => void;
  onPageChange: (page: number) => void;
}

/**
 * 训练记录列表栏：筛选后的记录总数、逐条摘要与分页；后台刷新保留已有内容并以 ``aria-busy`` 表达进行中。
 * 分页按响应的页码与总数翻页，翻页与筛选后由页面按当前结果重新确定选中记录。
 */
export default function WorkoutRecordList({
  list,
  error,
  fetching,
  selectedId,
  className,
  onSelect,
  onPageChange,
}: WorkoutRecordListProps) {
  // 分页取值与总数来自服务端回显；结果未到达时两侧按钮均禁用
  const page = list?.page ?? 1;
  const totalPages =
    list === undefined
      ? 1
      : Math.max(1, Math.ceil(list.total / list.page_size));
  return (
    <section
      aria-label="训练记录列表"
      aria-busy={fetching}
      className={cn("flex min-h-0 flex-col", className)}
    >
      {list !== undefined && (
        <p className="shrink-0 px-5 pt-1 pb-3 text-xs tabular-nums text-muted-foreground">
          {`共 ${list.total} 条`}
        </p>
      )}
      <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-3">
        {list === undefined ? (
          error !== null ? (
            <p role="alert" className="px-2 text-sm text-destructive">
              {error}
            </p>
          ) : (
            <ListSkeleton />
          )
        ) : list.items.length === 0 ? (
          <p role="status" className="px-2 text-sm">
            没有符合条件的训练记录。
          </p>
        ) : (
          <ul className="flex flex-col gap-2">
            {list.items.map((record) => {
              const active = record.id === selectedId;
              return (
                <li key={record.id}>
                  <button
                    type="button"
                    onClick={() => onSelect(record.id)}
                    aria-current={active ? "true" : undefined}
                    className={cn(
                      "w-full rounded-lg border p-4 text-left transition-colors",
                      "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50",
                      active
                        ? "border-primary bg-secondary"
                        : "border-border bg-card hover:bg-accent",
                    )}
                  >
                    <span className="block font-display text-lg leading-none font-light tracking-tight tabular-nums">
                      {record.performed_on}
                    </span>
                    <span className="mt-2 block text-xs tabular-nums text-muted-foreground">
                      {describeWorkoutContent(record.content)}
                    </span>
                    <span className="mt-2.5 flex flex-wrap gap-1.5">
                      {record.content.exercises.map((exercise, index) => (
                        <span
                          key={index}
                          className="max-w-40 truncate rounded-full border border-border bg-background px-2 py-0.5 text-xs"
                        >
                          {exercise.name}
                        </span>
                      ))}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>
      <div className="shrink-0 border-t border-border px-3 py-3">
        <Pagination>
          <PaginationContent>
            <PaginationItem>
              <Button
                type="button"
                variant="outline"
                size="icon"
                aria-label="上一页"
                disabled={fetching || page <= 1}
                onClick={() => onPageChange(page - 1)}
              >
                <ChevronLeft aria-hidden />
              </Button>
            </PaginationItem>
            {list !== undefined && (
              <PaginationItem>
                <span className="px-2 text-sm tabular-nums text-muted-foreground">
                  {`第 ${page} / ${totalPages} 页`}
                </span>
              </PaginationItem>
            )}
            <PaginationItem>
              <Button
                type="button"
                variant="outline"
                size="icon"
                aria-label="下一页"
                disabled={fetching || page >= totalPages}
                onClick={() => onPageChange(page + 1)}
              >
                <ChevronRight aria-hidden />
              </Button>
            </PaginationItem>
          </PaginationContent>
        </Pagination>
      </div>
    </section>
  );
}
