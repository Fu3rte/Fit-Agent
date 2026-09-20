import type { ReactNode } from "react";
import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { getProfile, getTrends, listPersonalBests } from "@/lib/api";
import PersonalBestPanel from "@/features/dashboard/components/PersonalBestPanel";
import WeightTrendCard from "@/features/dashboard/components/WeightTrendCard";
import ProfileCard from "@/features/dashboard/components/ProfileCard";

function Panel({
  query,
  label,
  children,
}: {
  query: Pick<UseQueryResult, "isError" | "isPending" | "error">;
  label: string;
  children: ReactNode;
}) {
  return (
    <div className="flex min-w-0 flex-col gap-2">
      {query.isError && (
        <p className="text-sm text-destructive">
          {label}加载失败：{query.error?.message}
        </p>
      )}
      {query.isPending && (
        <p className="text-sm text-muted-foreground">正在加载{label}…</p>
      )}
      {children}
    </div>
  );
}

export default function DashboardPage() {
  const bests = useQuery({
    queryKey: ["personal-bests"],
    queryFn: listPersonalBests,
  });
  const trends = useQuery({ queryKey: ["trends"], queryFn: getTrends });
  const profile = useQuery({ queryKey: ["profile"], queryFn: getProfile });

  return (
    <div className="mx-auto w-full max-w-5xl px-6 pt-10 pb-10">
      <div className="flex flex-col gap-6">
        {/* 上排：右列体重卡占更大比例（2:3），两卡各取自然高度 */}
        <div className="grid gap-6 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
          <Panel query={profile} label="画像">
            {profile.data && <ProfileCard profile={profile.data.profile} />}
          </Panel>

          <Panel query={trends} label="体重变化">
            {trends.data && <WeightTrendCard trends={trends.data.trends} />}
          </Panel>
        </div>

        <Panel query={bests} label="PB">
          {bests.data && (
            <PersonalBestPanel bests={bests.data.personal_bests} />
          )}
        </Panel>
      </div>
    </div>
  );
}
