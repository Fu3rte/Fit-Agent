import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { PenLine } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { getRecords } from "@/lib/api";
import type {
  RecordSet,
  SetJudgement,
  TrainingRecord,
  TrainingRevision,
} from "@/lib/contract";

/** 归属徽章：新增 / 更正 */
function KindBadge({ kind }: { kind: TrainingRecord["kind"] }) {
  return kind === "correction" ? (
    <Badge variant="default">更正</Badge>
  ) : (
    <Badge variant="secondary">新增</Badge>
  );
}

/** 状态徽章：valid / incomplete / voided（voided 整次退出统计但保留展示） */
function StatusBadge({ status }: { status: TrainingRecord["status"] }) {
  if (status === "voided")
    return <Badge variant="destructive">已作废</Badge>;
  if (status === "incomplete")
    return (
      <Badge variant="outline" className="border-dashed text-muted-foreground">
        待补全
      </Badge>
    );
  return <Badge variant="outline">有效</Badge>;
}

/** 组级三桶摘要徽章（有对照安排时展示现算结果，前端不重算） */
function JudgementBuckets({ b }: { b: NonNullable<TrainingRecord["judgement"]> }) {
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
      <span>组级判定：</span>
      <Badge variant="default">符合 {b.met}</Badge>
      <Badge variant="destructive">未符合 {b.unmet}</Badge>
      <Badge variant="outline" className="border-dashed">
        待补全 {b.pending}
      </Badge>
    </div>
  );
}

/** 组事实一行（只读摘要，共用 SetLine 逻辑但不展示判定徽章——旧修订无派生判定） */
function RevSetLine({ set }: { set: RecordSet }) {
  const parts: string[] = [];
  if (set.weight_kg !== undefined) parts.push(`${set.weight_kg}kg`);
  if (set.reps !== undefined) parts.push(`${set.reps} 次`);
  return (
    <span className="inline-flex items-center gap-2">
      <span className="text-muted-foreground">
        {set.set_type === "warmup" ? "热身" : "工作"}
      </span>
      <span className="tabular-nums">{parts.join(" · ") || "—"}</span>
      {set.assisted && <Badge variant="outline">有辅助</Badge>}
    </span>
  );
}

/**
 * 旧修订只读追溯行：修订确认时间、当时状态、组事实摘要、修订说明。
 * 只读展示，不可编辑、不可再提交（05 5.3 / F4-01 契约 TrainingRevision）。
 */
function RevisionRow({ rev }: { rev: TrainingRevision }) {
  return (
    <div className="flex flex-col gap-1 rounded-md border border-border/60 bg-muted/30 p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs tabular-nums text-muted-foreground">
          {rev.confirmed_at.replace("T", " ").slice(0, 19)}
        </span>
        <StatusBadge status={rev.status} />
      </div>
      <p className="text-xs text-muted-foreground">
        {rev.exercise} · {rev.variant}
      </p>
      <ul className="flex flex-col gap-0.5 text-xs">
        {rev.sets.map((set, i) => (
          <li key={i}>
            <RevSetLine set={set} />
          </li>
        ))}
      </ul>
      {rev.revision_note && (
        <p className="text-xs italic text-muted-foreground">
          修订说明：{rev.revision_note}
        </p>
      )}
    </div>
  );
}

/** 组级判定徽章：符合 / 未符合 / 待补全（基准=当次安排、只看次数） */
function SetJudgementBadge({ judgement }: { judgement: SetJudgement }) {
  if (judgement === "met") return <Badge variant="default">符合</Badge>;
  if (judgement === "unmet")
    return <Badge variant="destructive">未符合</Badge>;
  return (
    <Badge variant="outline" className="border-dashed text-muted-foreground">
      待补全
    </Badge>
  );
}

/**
 * 单组展示：重量 × 次数（主观余力字段已拍隐藏，不展示、不落库）；
 * 辅助标记异常申报制；有对照安排时带组级判定徽章。
 */
function SetLine({ set }: { set: RecordSet }) {
  const parts: string[] = [];
  if (set.weight_kg !== undefined) parts.push(`${set.weight_kg}kg`);
  if (set.reps !== undefined) parts.push(`${set.reps} 次`);
  return (
    <li className="flex items-center gap-2">
      <span className="text-muted-foreground">
        {set.set_type === "warmup" ? "热身" : "工作"}
      </span>
      <span className="tabular-nums">{parts.join(" · ")}</span>
      {set.assisted && <Badge variant="outline">有辅助</Badge>}
      {set.judgement && <SetJudgementBadge judgement={set.judgement} />}
    </li>
  );
}

/** 单条训练记录（只读；更正必须经对话草稿确认，不直接编辑） */
function RecordCard({ record }: { record: TrainingRecord }) {
  const navigate = useNavigate();
  const workingSets = record.sets.filter((s) => s.set_type === "working");
  // 旧修订：当前修订 id 即 record.id；其余为可只读追溯的历史修订
  const history = (record.revisions ?? []).filter((r) => r.id !== record.id);
  const isVoided = record.status === "voided";
  return (
    <Card className={isVoided ? "opacity-80" : undefined}>
      <CardHeader>
        <div className="flex flex-wrap items-center gap-2">
          {/* 日期用正文 sans：font-display 会兜底到系统 serif，数字观感突兀 */}
          <span className="text-lg leading-none tracking-tight tabular-nums">
            {record.date}
          </span>
          <KindBadge kind={record.kind} />
          <StatusBadge status={record.status} />
        </div>
        <CardDescription>
          {record.exercise} · {record.variant} ·{" "}
          {record.comparison
            ? `原计划 ${record.comparison.planned_sets ?? "?"} 组 · 当次安排 ${record.comparison.arranged_sets ?? "?"} 组 · 实际 ${workingSets.length} 组`
            : workingSets.length > 0
              ? `${workingSets.length} 个工作组 · 无对照安排`
              : "无工作组 · 无对照安排"}
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {record.warmup_summary && (
          <p className="text-sm text-muted-foreground">
            热身：{record.warmup_summary}
          </p>
        )}
        <ul className="flex flex-col gap-1 text-sm">
          {record.sets.map((set, i) => (
            <SetLine key={i} set={set} />
          ))}
        </ul>
        {/* 组级三桶摘要：现算结果，有对照安排时展示 */}
        {record.judgement && (
          <JudgementBuckets b={record.judgement} />
        )}
        {record.revision_note && (
          <p className="text-xs text-muted-foreground italic">
            修订说明：{record.revision_note}
          </p>
        )}
        {isVoided && (
          <p className="text-xs text-destructive">
            该记录已整次作废，退出完成率／三桶／PR 统计；旧修订仍可查。
          </p>
        )}
        {/* 旧修订只读追溯（时间与内容；不可编辑、不可再提交） */}
        {history.length > 0 && (
          <details className="rounded-md border border-border/60 p-3">
            <summary className="cursor-pointer text-sm text-muted-foreground select-none">
              历史修订（{history.length} 条 · 只读追溯）
            </summary>
            <div className="mt-3 flex flex-col gap-2">
              {history.map((rev) => (
                <RevisionRow key={rev.id} rev={rev} />
              ))}
            </div>
          </details>
        )}
        {/* 已作废为终态：不再提供「发起更正」入口 */}
        {!isVoided && (
          <div className="flex justify-end">
            <Button
              className="cursor-pointer"
              variant="outline"
              size="sm"
              onClick={() =>
                navigate("/", {
                  state: {
                    prefill: `我想更正 ${record.date} 的训练记录：${record.exercise} 数据有误，需要更正。`,
                  },
                })
              }
            >
              <PenLine className="size-3.5" aria-hidden />
              发起更正
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

/** /records 训练记录：只读列表，日期倒序 */
export default function RecordsPage() {
  const records = useQuery({ queryKey: ["records"], queryFn: getRecords });
  const sorted = records.data?.records
    ? [...records.data.records].sort((a, b) => b.date.localeCompare(a.date))
    : [];

  return (
    <div className="mx-auto w-full max-w-3xl px-6 pb-10">
      <header className="pt-10 pb-6">
        <h2 className="font-display text-3xl font-light tracking-tight">
          训练记录
        </h2>
        <p className="mt-1 text-sm text-muted-foreground">
          正式记录的只读查看；更正经对话草稿确认后生效并保留修订痕迹
        </p>
      </header>

      {records.isPending && (
        <p className="mt-10 text-sm text-muted-foreground">正在加载记录…</p>
      )}
      {records.isError && (
        <p className="mt-10 text-sm text-destructive">
          加载失败：{records.error.message}，请刷新重试。
        </p>
      )}

      {records.data && sorted.length === 0 && (
        <p className="mt-10 text-sm text-muted-foreground">
          暂无训练记录；到对话页用自然语言打卡，确认后即在此显示。
        </p>
      )}

      <div className="flex flex-col gap-4">
        {sorted.map((record) => (
          <RecordCard key={record.id} record={record} />
        ))}
      </div>
    </div>
  );
}
