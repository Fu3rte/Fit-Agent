import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { getTrends, listPersonalBests } from "@/lib/api";
import type { WorkoutGapWire } from "@/lib/contract";
import PersonalBestPanel from "@/features/dashboard/components/PersonalBestPanel";
import WeightTrendCard from "@/features/dashboard/components/WeightTrendCard";

/** 看板 Query key：记录页用全量 ``invalidateQueries()``，两个 key 都会随写入刷新 */
const PERSONAL_BESTS_KEY = ["personal-bests"];
const TRENDS_KEY = ["trends"];

function gapText(gap: WorkoutGapWire): string {
  if (gap.status === "no_data") return "暂无训练记录";
  return `距上次训练 ${gap.days} 天（${gap.last_performed_on}）`;
}

/** 两个 Query 的错误／加载态：同一段文案形状，只换称呼 */
function QueryStatus({
  query,
  label,
}: {
  query: Pick<UseQueryResult, "isError" | "isPending" | "error">;
  label: string;
}) {
  if (query.isError)
    return (
      <p className="text-sm text-destructive">
        {label}加载失败：{query.error?.message}
      </p>
    );
  if (query.isPending)
    return <p className="text-sm text-muted-foreground">正在加载{label}…</p>;
  return null;
}

export default function DashboardPage() {
  const bests = useQuery({
    queryKey: PERSONAL_BESTS_KEY,
    queryFn: listPersonalBests,
  });
  const trends = useQuery({ queryKey: TRENDS_KEY, queryFn: getTrends });

  const trendSummary = trends.data?.trends.trend_summary;

  return (
    <div className="mx-auto w-full max-w-7xl px-6 pb-10">
      <header className="flex flex-col gap-4 pt-10 pb-6 md:flex-row md:items-end md:justify-between">
        <div>
          <h2 className="font-display text-3xl font-light tracking-tight">
            数据看板
          </h2>
          {trendSummary && (
            <p className="mt-1 text-xs text-muted-foreground">
              {gapText(trendSummary.days_since_last_workout)}
            </p>
          )}
        </div>
      </header>

      <QueryStatus query={bests} label="PB" />
      <QueryStatus query={trends} label="体重变化" />

      <div className="flex flex-col gap-6">
        {bests.data && <PersonalBestPanel bests={bests.data.personal_bests} />}
        {trends.data && <WeightTrendCard trends={trends.data.trends} />}
      </div>
    </div>
  );
}
