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
import {
  getPlan,
  getRecordJudgement,
  getRecords,
  getStatsCompletion,
  getStatsPr,
  listReviews,
} from "@/lib/api";
import type {
  Buckets,
  PrEntry,
  ReviewBasisWire,
  ReviewWire,
  WeekCompletion,
} from "@/lib/contract";
import {
  addBuckets,
  emptyBuckets,
  mapPrDisplay,
  mapWeekCompletion,
  pickLatestReview,
  planPrKeys,
  recordPrKeys,
} from "@/lib/readModels";

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

/** 前端聚合统计（F7）：completion 双周查询 + PR 按记录/计划键现算 + 三桶 judgements 汇总 */
interface StatsAggregate {
  per_week: WeekCompletion[];
  buckets: Buckets;
  prs: PrEntry[];
  /** 展示用：取复盘/记录可得的最晚时间；无数据时 null */
  data_updated_at: string | null;
}

async function fetchStatsAggregate(): Promise<StatsAggregate> {
  const [planRes, recordsRes, reviewsRes] = await Promise.all([
    getPlan(),
    getRecords(),
    listReviews(),
  ]);

  /* 完成率：当前计划各 Wn 逐周查询；无到期名额 completion:null → 「暂无」行不编造 */
  const per_week: WeekCompletion[] = [];
  const planView = planRes.plan;
  let dataUpdatedAt: string | null = pickLatestReview(reviewsRes.reviews)
    ?.generated_at ?? null;

  for (const r of recordsRes.records) {
    if (!dataUpdatedAt || r.created_at > dataUpdatedAt)
      dataUpdatedAt = r.created_at;
  }

  if (planView) {
    const startMs = Date.parse(`${planView.starts_on}T00:00:00Z`);
    const reviewMs = Date.parse(`${planView.review_on}T00:00:00Z`);
    const totalDays = Math.max(
      0,
      Math.ceil((reviewMs - startMs) / 86_400_000),
    );
    const weekCount = Math.max(1, Math.ceil(totalDays / 7));
    for (let weekNo = 1; weekNo <= weekCount; weekNo += 1) {
      const { completion } = await getStatsCompletion(planView.id, weekNo);
      if (completion) per_week.push(mapWeekCompletion(completion));
    }
  }

  /* 三桶：逐身份 judgement 汇总（无对照/已作废 judgement:null 不计） */
  let buckets = emptyBuckets();
  const judgements = await Promise.all(
    recordsRes.records.map((r) => getRecordJudgement(r.id)),
  );
  for (const j of judgements) {
    if (j.judgement?.has_comparison)
      buckets = addBuckets(buckets, j.judgement.buckets);
  }

  /* PR：从记录 + 计划收集 (exercise_id, load_notation) 键后逐键现算 */
  const keys = new Map<string, { exercise_id: string; load_notation: string }>();
  const planKeys = planView ? planPrKeys(planView.plan) : [];
  for (const k of [...planKeys, ...recordPrKeys(recordsRes.records)]) {
    keys.set(`${k.exercise_id}|${k.load_notation}`, k);
  }
  const prs: PrEntry[] = [];
  for (const key of keys.values()) {
    const { pr } = await getStatsPr(key.exercise_id, key.load_notation);
    const mapped = mapPrDisplay(pr);
    if (mapped && mapped.best_weight_kg !== null) {
      prs.push({
        exercise: mapped.exercise,
        variant: mapped.variant,
        best_weight_kg: mapped.best_weight_kg,
        // best_reps 在「最高重量」查询下为 null：展示时不编造 0 次
        best_reps_at_weight: mapped.best_reps_at_weight ?? 0,
      });
    }
  }
  prs.sort((a, b) => a.exercise.localeCompare(b.exercise));

  return { per_week, buckets, prs, data_updated_at: dataUpdatedAt };
}

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
        <div className="text-sm tabular-nums">
          {pr.best_reps_at_weight > 0
            ? `${pr.best_weight_kg}kg × ${pr.best_reps_at_weight} 次`
            : `${pr.best_weight_kg}kg`}
        </div>
      </div>
    </div>
  );
}

/**
 * 依据快照摘要（F5-03 / F8）：只读展示该条复盘生成时冻结的 per_week／PR；
 * 后端 basis 无 buckets/data_updated_at，不编造。
 */
function BasisSummary({ basis }: { basis: ReviewBasisWire }) {
  const weekLine = basis.per_week
    .map((w) => `W${w.week_no} ${w.numerator}/${w.denominator}`)
    .join(" · ");
  const prLine = basis.prs
    .map((p) => `${p.exercise_id} ${Math.round(p.load_kg_key) / 1000}kg × ${p.best_reps}`)
    .join(" · ");
  return (
    <div className="mt-4 space-y-1.5 rounded-md border border-border/60 bg-secondary/30 px-3.5 py-3">
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-xs font-medium text-muted-foreground">
          依据快照（生成时冻结）
        </span>
        <span className="text-xs text-muted-foreground tabular-nums">
          快照 schema v{basis.schema_version}
        </span>
      </div>
      <p className="text-xs text-muted-foreground">
        完成率：{weekLine || "暂无"}
      </p>
      <p className="text-xs text-muted-foreground">PR：{prLine || "暂无"}</p>
    </div>
  );
}

/** 复盘正文 + 生成时间 + stale（F8：body_markdown；最新条=当前复盘） */
function ReviewBody({ review }: { review: ReviewWire }) {
  return (
    <>
      {review.body_markdown ? (
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
          {review.body_markdown}
        </Markdown>
      ) : (
        <p className="text-sm text-muted-foreground">
          暂无复盘沉淀，可在对话中生成。
        </p>
      )}
      {review.basis ? (
        <BasisSummary basis={review.basis} />
      ) : (
        <p className="mt-4 text-xs text-muted-foreground">
          暂无依据快照（尚未生成正式复盘）；页面不编造完成率与 PR。
        </p>
      )}
    </>
  );
}

export default function ReviewPage() {
  const navigate = useNavigate();
  const stats = useQuery({ queryKey: ["stats-agg"], queryFn: fetchStatsAggregate });
  const reviews = useQuery({ queryKey: ["reviews"], queryFn: listReviews });
  const latestReview = pickLatestReview(reviews.data?.reviews);

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
              {stats.isPending && (
                <p className="text-sm text-muted-foreground">正在加载…</p>
              )}
              {stats.isError && (
                <p className="text-sm text-destructive">
                  加载失败：{stats.error.message}
                </p>
              )}
              {stats.data?.per_week.map((row) => (
                <WeekRow key={row.week} row={row} />
              ))}
              {stats.data && stats.data.per_week.length === 0 && (
                <p className="text-sm text-muted-foreground">暂无完成率</p>
              )}
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
              <CardDescription>正式有效记录 · 同动作同口径</CardDescription>
            </CardHeader>
            <CardContent className="divide-y divide-border/60">
              {stats.data?.prs.map((pr) => (
                <PrRow key={`${pr.exercise}-${pr.variant}`} pr={pr} />
              ))}
              {stats.data && stats.data.prs.length === 0 && (
                <p className="text-sm text-muted-foreground">暂无 PR</p>
              )}
            </CardContent>
          </Card>
        </div>

        {/* 复盘沉淀卡 */}
        <Card>
          <CardHeader className="flex-row items-start justify-between gap-4">
            <div className="flex flex-col gap-1.5">
              <CardTitle>复盘沉淀</CardTitle>
              <CardDescription>
                {latestReview
                  ? `生成于 ${latestReview.generated_at}`
                  : "Agent 复盘文本"}
              </CardDescription>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              {latestReview?.stale && (
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
            {reviews.isPending && (
              <p className="text-sm text-muted-foreground">正在加载复盘…</p>
            )}
            {reviews.isError && (
              <p className="text-sm text-destructive">
                加载失败：{reviews.error.message}
              </p>
            )}
            {latestReview ? (
              <ReviewBody review={latestReview} />
            ) : (
              reviews.data && (
                <p className="text-sm text-muted-foreground">
                  暂无复盘沉淀，可在对话中生成。
                </p>
              )
            )}
          </CardContent>
        </Card>

        {/* 页脚：数据更新时间（PRD 5.8）；无数据不编造 */}
        <p className="pb-2 text-xs text-muted-foreground">
          {stats.isPending
            ? "数据加载中…"
            : stats.data?.data_updated_at
              ? `数据更新时间：${stats.data.data_updated_at}`
              : "暂无数据更新时间"}
        </p>
      </div>
    </div>
  );
}
