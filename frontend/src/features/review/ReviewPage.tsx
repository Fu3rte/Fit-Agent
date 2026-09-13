import type * as React from "react";
import { useQuery } from "@tanstack/react-query";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useNavigate } from "react-router-dom";
import { MessageSquarePlus, RefreshCw } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { getReview, getStats } from "@/lib/api";
import type {
  Buckets,
  PrEntry,
  ReviewBasis,
  WeekCompletion,
} from "@/lib/contract";

/* A3 白名单：与对话页同一套元素范围（p/标题/列表/表格/强调/代码/引用） */
const markdownComponents = {
  table: ({ className, ...props }: React.ComponentProps<"table">) => (
    <table
      className={`w-full border-collapse overflow-hidden rounded-md ${className ?? ""}`}
      {...props}
    />
  ),
  th: ({ className, ...props }: React.ComponentProps<"th">) => (
    <th
      className={`border-b border-border px-3 py-1.5 text-left text-xs font-medium ${className ?? ""}`}
      {...props}
    />
  ),
  td: ({ className, ...props }: React.ComponentProps<"td">) => (
    <td
      className={`border-b border-border/60 px-3 py-1.5 text-sm ${className ?? ""}`}
      {...props}
    />
  ),
  blockquote: ({ className, ...props }: React.ComponentProps<"blockquote">) => (
    <blockquote
      className={`border-l-2 border-border pl-3 text-sm text-muted-foreground ${className ?? ""}`}
      {...props}
    />
  ),
  code: ({ className, ...props }: React.ComponentProps<"code">) => (
    <code
      className={`rounded-sm bg-secondary px-1 py-0.5 text-[0.85em] ${className ?? ""}`}
      {...props}
    />
  ),
};

/* rate 为 null 时按口径显示「暂无」，绝不显示 0%/100%（PRD 5.8） */
function WeekRow({ row }: { row: WeekCompletion }) {
  return (
    <div className="flex items-baseline justify-between gap-4 py-1.5">
      <div className="text-sm">
        <span className="font-medium">{row.week}</span>
        <span className="ml-2 text-xs text-muted-foreground">
          应训练 {row.planned} · 已完成 {row.completed}
        </span>
      </div>
      <span className="text-xl font-light tabular-nums">
        {row.rate === null ? (
          <span className="text-sm text-muted-foreground">暂无</span>
        ) : (
          `${row.rate}%`
        )}
      </span>
    </div>
  );
}

/* 三桶：统一用语「符合处方目标的工作组」，只展示组数，不用百分比（PRD 5.8） */
const bucketItems: Array<{
  key: keyof Buckets;
  label: string;
  dot: string;
}> = [
  { key: "met", label: "符合目标", dot: "bg-[var(--chart-1)]" },
  { key: "unmet", label: "未符合", dot: "bg-[var(--destructive)]" },
  { key: "pending", label: "待补全", dot: "bg-[var(--chart-2)]" },
];

function BucketRow({ buckets }: { buckets: Buckets }) {
  return (
    <div className="flex flex-col gap-2">
      {bucketItems.map(({ key, label, dot }) => (
        <div key={key} className="flex items-center justify-between">
          <span className="flex items-center gap-2 text-sm">
            <span aria-hidden className={`size-2 rounded-full ${dot}`} />
            {label}
          </span>
          <span className="text-xl font-light tabular-nums">
            {buckets[key]}
          </span>
        </div>
      ))}
    </div>
  );
}

function PrRow({ pr }: { pr: PrEntry }) {
  return (
    <div className="flex items-baseline justify-between gap-4 py-1.5">
      <div className="text-sm">
        <span className="font-medium">{pr.exercise}</span>
        <span className="ml-1 text-xs text-muted-foreground">{pr.variant}</span>
      </div>
      <div className="text-right">
        {/* 最高重量及该重量下单组最高完成次数（PRD 5.8 同一口径，不重复展示） */}
        <div className="text-sm tabular-nums">
          {pr.best_weight_kg}kg × {pr.best_reps_at_weight} 次
        </div>
      </div>
    </div>
  );
}

/**
 * 依据快照摘要（F5-03）：只读展示该条复盘生成时冻结的 per_week／三桶／PR／
 * data_updated_at；不与现算统计混算，无 basis 时空态说明、不编造。
 */
function BasisSummary({ basis }: { basis: ReviewBasis }) {
  const weekLine = basis.per_week
    .map((w) => `${w.week} ${w.completed}/${w.planned}`)
    .join(" · ");
  const prLine = basis.prs
    .map((p) => `${p.exercise} ${p.best_weight_kg}kg × ${p.best_reps_at_weight}`)
    .join(" · ");
  return (
    <div className="mt-4 space-y-1.5 rounded-md border border-border/60 bg-secondary/30 px-3.5 py-3">
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-xs font-medium text-muted-foreground">
          依据快照（生成时冻结）
        </span>
        <span className="text-xs text-muted-foreground tabular-nums">
          依据数据时间：{basis.data_updated_at}
        </span>
      </div>
      <p className="text-xs text-muted-foreground">
        完成率：{weekLine || "暂无"}
      </p>
      <p className="text-xs text-muted-foreground">
        三桶：符合 {basis.buckets.met} 组 · 未符合 {basis.buckets.unmet} 组 ·
        待补全 {basis.buckets.pending} 组
      </p>
      <p className="text-xs text-muted-foreground">PR：{prLine || "暂无"}</p>
    </div>
  );
}

export default function ReviewPage() {
  const navigate = useNavigate();
  const stats = useQuery({ queryKey: ["stats"], queryFn: getStats });
  const review = useQuery({ queryKey: ["review"], queryFn: getReview });

  return (
    <div className="mx-auto w-full max-w-3xl px-6">
      <header className="pt-10 pb-6">
        <h2 className="font-display text-3xl font-light tracking-tight">
          统计与复盘
        </h2>
        <p className="mt-1 text-sm text-muted-foreground">
          数值由确定性计算提供；Agent 复盘只做解释，不改写统计。
        </p>
      </header>

      <div className="flex flex-col gap-4 pb-10">
        {/* 统计卡区 */}
        <div className="grid gap-4 md:grid-cols-3">
          <Card>
            <CardHeader>
              <CardTitle>训练完成率</CardTitle>
              <CardDescription>按计划周 Wn · 按训练次数口径</CardDescription>
            </CardHeader>
            <CardContent className="divide-y divide-border/60">
              {stats.data?.per_week.map((row) => (
                <WeekRow key={row.week} row={row} />
              ))}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>符合处方目标的工作组</CardTitle>
              <CardDescription>组级三桶判定 · 组数</CardDescription>
            </CardHeader>
            <CardContent>
              {stats.data && <BucketRow buckets={stats.data.buckets} />}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>个人纪录 PR</CardTitle>
              <CardDescription>正式有效记录 · 同动作同变式</CardDescription>
            </CardHeader>
            <CardContent className="divide-y divide-border/60">
              {stats.data?.prs.map((pr) => (
                <PrRow key={`${pr.exercise}-${pr.variant}`} pr={pr} />
              ))}
            </CardContent>
          </Card>
        </div>

        {/* 复盘沉淀卡 */}
        <Card>
          <CardHeader className="flex-row items-start justify-between gap-4">
            <div className="flex flex-col gap-1.5">
              <CardTitle>复盘沉淀</CardTitle>
              <CardDescription>
                {review.data
                  ? `生成于 ${review.data.generated_at}`
                  : "Agent 复盘文本"}
              </CardDescription>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              {review.data?.stale && (
                <Badge
                  variant="outline"
                  className="gap-1 text-amber-600 dark:text-amber-400"
                >
                  <RefreshCw className="size-3" />
                  依据已变更 · 可重新生成
                </Badge>
              )}
              <Button
                size="sm"
                onClick={() =>
                  navigate("/", {
                    state: { prefill: "请基于最新数据生成训练复盘。" },
                  })
                }
              >
                <MessageSquarePlus />
                在对话中生成复盘
              </Button>
            </div>
          </CardHeader>
          <CardContent className="prose-review text-sm">
            {review.data?.text ? (
              <Markdown
                remarkPlugins={[remarkGfm]}
                allowedElements={[
                  "p",
                  "h1",
                  "h2",
                  "h3",
                  "ul",
                  "ol",
                  "li",
                  "table",
                  "thead",
                  "tbody",
                  "tr",
                  "th",
                  "td",
                  "strong",
                  "em",
                  "code",
                  "blockquote",
                  "br",
                  "hr",
                ]}
                unwrapDisallowed
                components={markdownComponents}
              >
                {review.data.text}
              </Markdown>
            ) : (
              <p className="text-sm text-muted-foreground">
                暂无复盘沉淀，可在对话中生成。
              </p>
            )}
            {/* 依据快照摘要：只读冻结事实；空态不编造（F5-03） */}
            {review.data?.basis ? (
              <BasisSummary basis={review.data.basis} />
            ) : (
              review.data && (
                <p className="mt-4 text-xs text-muted-foreground">
                  暂无依据快照（空数据种子或尚未生成正式复盘）；页面不编造完成率与
                  PR。
                </p>
              )
            )}
          </CardContent>
        </Card>

        {/* 页脚：数据更新时间（PRD 5.8） */}
        <p className="pb-2 text-xs text-muted-foreground">
          {stats.data
            ? `数据更新时间：${stats.data.data_updated_at}`
            : "数据加载中…"}
        </p>
      </div>
    </div>
  );
}
