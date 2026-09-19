import { useRef, useState, type ComponentType, type MouseEvent } from "react";
import {
  ChevronLeft,
  ChevronRight,
  Dumbbell,
  Search,
  Star,
  Timer,
  Trophy,
  Zap,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Pagination,
  PaginationContent,
  PaginationEllipsis,
  PaginationItem,
  PaginationLink,
  PaginationNext,
  PaginationPrevious,
} from "@/components/ui/pagination";
import type { PersonalBestTypeWire, PersonalBestWire } from "@/lib/contract";

/** 招牌展台置顶集合的存储键（逗号分隔的 PB 主键） */
const PIN_STORAGE_KEY = "fitagent-dashboard-pins";

type DimensionFilter = "all" | PersonalBestTypeWire;

const PB_TYPE_STYLE: Record<
  PersonalBestTypeWire,
  {
    label: string;
    unit: string;
    Icon: ComponentType<{ className?: string }>;
    text: string;
    border: string;
    bg: string;
  }
> = {
  weight_pb: {
    label: "负重",
    unit: "kg",
    Icon: Dumbbell,
    text: "text-amber-600 dark:text-amber-400",
    border: "border-amber-500/30",
    bg: "bg-amber-500/10",
  },
  duration_pb: {
    label: "静态",
    unit: "秒",
    Icon: Timer,
    text: "text-emerald-600 dark:text-emerald-400",
    border: "border-emerald-500/30",
    bg: "bg-emerald-500/10",
  },
  reps_pb: {
    label: "次数",
    unit: "次",
    Icon: Zap,
    text: "text-sky-600 dark:text-sky-400",
    border: "border-sky-500/30",
    bg: "bg-sky-500/10",
  },
};

const DIMENSION_TABS: Array<{ key: DimensionFilter; label: string }> = [
  { key: "all", label: "全部" },
  ...(Object.keys(PB_TYPE_STYLE) as PersonalBestTypeWire[]).map((key) => ({
    key,
    label: PB_TYPE_STYLE[key].label,
  })),
];

export const PB_PAGE_SIZE = 5;

/** 页码窗口：首末页常驻，当前页左右各一页，跳过的区间折成 ``gap`` */
export function pageNumbers(
  current: number,
  count: number,
): Array<number | "gap"> {
  const kept = [...new Set([1, count, current - 1, current, current + 1])]
    .filter((page) => page >= 1 && page <= count)
    .sort((left, right) => left - right);
  const result: Array<number | "gap"> = [];
  for (const [index, page] of kept.entries()) {
    if (index > 0 && page - kept[index - 1] > 1) result.push("gap");
    result.push(page);
  }
  return result;
}

/** PB 键含负重口径：同动作可因口径不同产出多条重量 PB，漏口径会撞键 */
function pbKey(pb: PersonalBestWire): string {
  return `${pb.exercise_id}-${pb.pb_type}-${pb.load_convention ?? "none"}`;
}

/** 置顶集合：存储缺失或为空都视为空集合；顺序在渲染时按 PB 列表重排 */
function readPinnedKeys(): string[] {
  if (typeof localStorage === "undefined") return [];
  const stored = localStorage.getItem(PIN_STORAGE_KEY);
  return stored === null || stored === "" ? [] : stored.split(",");
}

function writePinnedKeys(keys: string[]): void {
  localStorage.setItem(PIN_STORAGE_KEY, keys.join(","));
}

export default function PersonalBestPanel({
  bests,
}: {
  bests: PersonalBestWire[];
}) {
  const [keyword, setKeyword] = useState("");
  const [dimension, setDimension] = useState<DimensionFilter>("all");
  const [page, setPage] = useState(1);
  const [pinnedKeys, setPinnedKeys] = useState<string[]>(readPinnedKeys);
  const carouselRef = useRef<HTMLDivElement>(null);

  const trimmed = keyword.trim().toLowerCase();
  const filtered = bests.filter(
    (pb) =>
      (dimension === "all" || pb.pb_type === dimension) &&
      (trimmed === "" || pb.exercise_name.toLowerCase().includes(trimmed)),
  );
  const pinned = bests.filter((pb) => pinnedKeys.includes(pbKey(pb)));

  /* 页码取当前与总页数的较小值：记录被删或筛选变化后页数会缩 */
  const pageCount = Math.max(1, Math.ceil(filtered.length / PB_PAGE_SIZE));
  const currentPage = Math.min(page, pageCount);
  const pageItems = filtered.slice(
    (currentPage - 1) * PB_PAGE_SIZE,
    currentPage * PB_PAGE_SIZE,
  );

  const togglePin = (key: string) => {
    const next = pinnedKeys.includes(key)
      ? pinnedKeys.filter((item) => item !== key)
      : [...pinnedKeys, key];
    setPinnedKeys(next);
    writePinnedKeys(next);
  };

  const scrollCarousel = (direction: "left" | "right") => {
    carouselRef.current?.scrollBy({
      left: direction === "left" ? -240 : 240,
      behavior: "smooth",
    });
  };

  const nav = (target: number, enabled: boolean) => ({
    href: "#",
    "aria-disabled": !enabled,
    className: enabled ? "" : "pointer-events-none opacity-50",
    onClick: (event: MouseEvent<HTMLAnchorElement>) => {
      event.preventDefault();
      setPage(target);
    },
  });

  return (
    <Card className="flex flex-col">
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <div>
            <CardTitle className="flex items-center gap-2">
              <Trophy className="size-4" />
              个人最好成绩（PB）
            </CardTitle>
          </div>
          <Badge variant="outline" className="tabular-nums">
            已收录 {bests.length} 项
          </Badge>
        </div>
      </CardHeader>

      <CardContent className="flex flex-1 flex-col gap-4">
        <section className="flex flex-col gap-2">
          <div
            ref={carouselRef}
            className="flex gap-3 overflow-x-auto pt-1 pb-2"
            style={{ scrollbarWidth: "none", msOverflowStyle: "none" }}
          >
            {pinned.map((pb) => {
              const style = PB_TYPE_STYLE[pb.pb_type];
              return (
                <div
                  key={pbKey(pb)}
                  className={`flex w-40 shrink-0 flex-col justify-between gap-2 rounded-xl border bg-gradient-to-b from-card to-muted/20 p-3 ${style.border}`}
                >
                  <div className="flex items-start justify-between gap-1">
                    <Badge variant="secondary" className="text-[10px]">
                      {style.label}
                    </Badge>
                    <button
                      type="button"
                      className="text-amber-500"
                      title="取消展台置顶"
                      aria-label={`取消展台置顶：${pb.exercise_name}`}
                      onClick={() => togglePin(pbKey(pb))}
                    >
                      <Star className="size-3.5 fill-current" />
                    </button>
                  </div>

                  <p
                    className="truncate text-xs font-medium"
                    title={pb.exercise_name}
                  >
                    {pb.exercise_name}
                  </p>

                  <div className="font-mono text-2xl font-bold tabular-nums">
                    {pb.value}
                    <span className="ml-0.5 text-xs font-normal text-muted-foreground">
                      {style.unit}
                    </span>
                  </div>

                  <div className="flex items-center justify-between border-t border-border/60 pt-1 text-[10px] text-muted-foreground">
                    <span className={`flex items-center gap-1 ${style.text}`}>
                      <style.Icon className="size-3.5" />
                      {style.label}
                    </span>
                    <span>{pb.performed_on.slice(5)}</span>
                  </div>
                </div>
              );
            })}

            {pinned.length === 0 && (
              <p className="w-full rounded-xl border border-dashed py-6 text-center text-xs text-muted-foreground">
                暂无展台动作，在下方列表点击 ⭐ 置顶。
              </p>
            )}
          </div>

          <div className="flex justify-end items-center gap-1">
            <Button
              variant="outline"
              size="icon"
              className="size-7"
              aria-label="展台向左滚动"
              onClick={() => scrollCarousel("left")}
            >
              <ChevronLeft className="size-3.5" />
            </Button>
            <Button
              variant="outline"
              size="icon"
              className="size-7"
              aria-label="展台向右滚动"
              onClick={() => scrollCarousel("right")}
            >
              <ChevronRight className="size-3.5" />
            </Button>
          </div>
        </section>

        <section className="flex flex-col gap-2 border-t pt-3">
          <div className="flex flex-col gap-2 sm:flex-row">
            <div className="relative flex-1">
              <Search className="absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={keyword}
                onChange={(event) => {
                  setKeyword(event.target.value);
                  setPage(1);
                }}
                placeholder="搜索动作名称…"
                className="h-9 pl-8 text-xs"
              />
            </div>
            <div className="flex shrink-0 rounded-full bg-muted p-0.5">
              {DIMENSION_TABS.map((tab) => (
                <button
                  key={tab.key}
                  type="button"
                  onClick={() => {
                    setDimension(tab.key);
                    setPage(1);
                  }}
                  className={`rounded-full px-3 py-1 text-xs transition-colors ${dimension === tab.key
                    ? "bg-card font-medium text-foreground shadow-sm"
                    : "text-muted-foreground hover:text-foreground"
                    }`}
                >
                  {tab.label}
                </button>
              ))}
            </div>
          </div>

          {/* 行高固定 h-14、容器最小高 18.5rem（5 行 × 3.5rem + 4 × gap-1）：末页不足 5 条时高度不变，翻页不会跳 */}
          <div
            className={`flex min-h-[18.5rem] flex-col gap-1 ${filtered.length === 0 ? "justify-center" : ""
              }`}
          >
            {pageItems.map((pb) => {
              const style = PB_TYPE_STYLE[pb.pb_type];
              const isPinned = pinnedKeys.includes(pbKey(pb));

              return (
                <div
                  key={pbKey(pb)}
                  className="flex h-14 items-center justify-between gap-2 rounded-lg border border-transparent p-2.5 transition-colors hover:border-border hover:bg-muted/40"
                >
                  <div className="flex min-w-0 items-center gap-2">
                    <button
                      type="button"
                      className="shrink-0 p-1"
                      title={isPinned ? "从展台移除" : "加入展台"}
                      aria-label={`${isPinned ? "从展台移除" : "加入展台"}：${pb.exercise_name}`}
                      onClick={() => togglePin(pbKey(pb))}
                    >
                      <Star
                        className={`size-4 ${isPinned
                          ? "fill-amber-500 text-amber-500"
                          : "text-muted-foreground/50"
                          }`}
                      />
                    </button>
                    <Badge variant="outline" className="shrink-0 text-[10px]">
                      {style.label}
                    </Badge>
                    <div className="min-w-0">
                      <p className="truncate text-sm font-medium">
                        {pb.exercise_name}
                      </p>
                      <p className="truncate text-[10px] text-muted-foreground">
                        {pb.performed_on} · 训练 #{pb.workout_session_id} 第{" "}
                        {pb.set_no} 组
                      </p>
                    </div>
                  </div>

                  <div className="flex shrink-0 items-center gap-2">
                    <span className="font-mono font-bold tabular-nums">
                      {pb.value}
                      <span className="ml-0.5 text-xs font-normal text-muted-foreground">
                        {style.unit}
                      </span>
                    </span>
                    <span
                      className={`rounded-md p-1 ${style.bg} ${style.text}`}
                      title={style.label}
                    >
                      <style.Icon className="size-3.5" />
                    </span>
                  </div>
                </div>
              );
            })}

            {filtered.length === 0 && (
              <p className="py-10 text-center text-xs text-muted-foreground">
                {bests.length === 0
                  ? "还没有可计入 PB 的有效工作组。"
                  : "未找到匹配的动作记录。"}
              </p>
            )}
          </div>

          {pageCount > 1 && (
            <Pagination className="pt-1">
              <PaginationContent>
                <PaginationItem>
                  <PaginationPrevious
                    text="上一页"
                    {...nav(currentPage - 1, currentPage > 1)}
                  />
                </PaginationItem>

                {pageNumbers(currentPage, pageCount).map((entry, index) => (
                  <PaginationItem
                    key={entry === "gap" ? `gap-${index}` : entry}
                  >
                    {entry === "gap" ? (
                      <PaginationEllipsis />
                    ) : (
                      <PaginationLink
                        href="#"
                        isActive={entry === currentPage}
                        onClick={(event) => {
                          event.preventDefault();
                          setPage(entry);
                        }}
                      >
                        {entry}
                      </PaginationLink>
                    )}
                  </PaginationItem>
                ))}

                <PaginationItem>
                  <PaginationNext
                    text="下一页"
                    {...nav(currentPage + 1, currentPage < pageCount)}
                  />
                </PaginationItem>
              </PaginationContent>
            </Pagination>
          )}
        </section>
      </CardContent>
    </Card>
  );
}
