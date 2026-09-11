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
import type { RecordSet, TrainingRecord } from "@/lib/contract";

/** 归属徽章：新增 / 更正 */
function KindBadge({ kind }: { kind: TrainingRecord["kind"] }) {
  return kind === "correction" ? (
    <Badge variant="default">更正</Badge>
  ) : (
    <Badge variant="secondary">新增</Badge>
  );
}

/** 状态徽章：正式 / 待补全（待补全不参与 PR 与完成率） */
function StatusBadge({ status }: { status: TrainingRecord["status"] }) {
  return status === "pending_completion" ? (
    <Badge variant="outline" className="border-dashed text-muted-foreground">
      待补全
    </Badge>
  ) : (
    <Badge variant="outline">正式</Badge>
  );
}

/** 单组展示：重量 × 次数 · RIR（未报告显示 —，不补造）；辅助标记异常申报制 */
function SetLine({ set }: { set: RecordSet }) {
  const parts: string[] = [];
  if (set.weight_kg !== undefined) parts.push(`${set.weight_kg}kg`);
  if (set.reps !== undefined) parts.push(`${set.reps} 次`);
  parts.push(`RIR ${set.rir ?? "—"}`);
  return (
    <li className="flex items-center gap-2">
      <span className="text-muted-foreground">
        {set.set_type === "warmup" ? "热身" : "工作"}
      </span>
      <span className="tabular-nums">{parts.join(" × ")}</span>
      {set.assisted && <Badge variant="outline">有辅助</Badge>}
    </li>
  );
}

/** 单条训练记录（只读；更正必须经对话草稿确认，不直接编辑） */
function RecordCard({ record }: { record: TrainingRecord }) {
  const navigate = useNavigate();
  const workingSets = record.sets.filter((s) => s.set_type === "working");
  return (
    <Card>
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
          {workingSets.length > 0
            ? `${workingSets.length} 个工作组`
            : "无工作组"}
          {record.schedule_snapshot
            ? ` · 对照安排：${record.schedule_snapshot}`
            : " · 无对照安排"}
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
        {record.revision_note && (
          <p className="text-xs text-muted-foreground italic">
            修订说明：{record.revision_note}
          </p>
        )}
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
