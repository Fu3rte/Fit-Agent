import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ArrowRight, ShieldAlert } from "lucide-react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { getProfile } from "@/lib/api";
import type {
  PlanBlock,
  PlanVersion,
  Profile,
  Restriction,
} from "@/lib/contract";

const WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

function Loading({ text }: { text: string }) {
  return <p className="mt-10 text-sm text-muted-foreground">{text}…</p>;
}

function LoadError({ text }: { text: string }) {
  return (
    <p className="mt-10 text-sm text-destructive">
      加载失败：{text}，请刷新重试。
    </p>
  );
}

/** 档案卡：目标/经验/频率/时长/器械/体重（PRD 5.2，只读） */
function ProfileCard({ profile }: { profile: Profile }) {
  const rows: Array<[string, string]> = [
    ["训练目标", profile.goal],
    ["训练经验", profile.experience],
    ["每周频率", `${profile.weekly_frequency} 次 / 周`],
    ["单次时长", `${profile.session_minutes} 分钟`],
    ["体重", `${profile.body_weight_kg} kg`],
  ];
  return (
    <Card>
      <CardHeader>
        <CardTitle>档案</CardTitle>
        <CardDescription>已确认的训练档案事实</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2.5 text-sm">
        {rows.map(([label, value]) => (
          <div
            key={label}
            className="flex items-baseline justify-between gap-4"
          >
            <span className="shrink-0 text-muted-foreground">{label}</span>
            <span className="text-right font-medium">{value}</span>
          </div>
        ))}
        <div className="flex items-baseline justify-between gap-4">
          <span className="shrink-0 text-muted-foreground">可用器械</span>
          <span className="flex flex-wrap justify-end gap-1.5">
            {profile.equipment.map((item) => (
              <Badge key={item} variant="secondary">
                {item}
              </Badge>
            ))}
          </span>
        </div>
      </CardContent>
    </Card>
  );
}

/** 动作限制卡：徽章，红色（destructive）= 暂禁 */
function RestrictionsCard({ restrictions }: { restrictions: Restriction[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>动作限制</CardTitle>
        <CardDescription>红色为暂禁；增删须在对话页经草稿确认</CardDescription>
      </CardHeader>
      <CardContent>
        {restrictions.length === 0 ? (
          <p className="text-sm text-muted-foreground">暂无动作限制</p>
        ) : (
          <ul className="flex flex-wrap gap-2">
            {restrictions.map((r) => (
              <li key={r.name}>
                <Badge
                  variant={r.restricted ? "destructive" : "secondary"}
                  title={r.note}
                  className="gap-1 py-1"
                >
                  {r.restricted && (
                    <ShieldAlert className="size-3" aria-hidden />
                  )}
                  {r.name}
                  {r.restricted && " · 暂禁"}
                </Badge>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

/** 单个板块（推/拉/腿）的动作表 */
function BlockTable({ block }: { block: PlanBlock }) {
  return (
    <div>
      <div className="mb-2 flex items-baseline justify-between">
        <h4 className="text-sm font-medium">{block.name}</h4>
        <span className="text-xs text-muted-foreground">
          每周{WEEKDAYS[block.weekday - 1] ?? `第 ${block.weekday} 天`}
        </span>
      </div>
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b text-left text-xs text-muted-foreground">
            <th className="py-1.5 pr-3 font-normal">动作</th>
            <th className="py-1.5 pr-3 font-normal">组 × 次</th>
            <th className="py-1.5 pr-3 font-normal">目标 RIR</th>
            <th className="py-1.5 font-normal">渐进方式</th>
          </tr>
        </thead>
        <tbody>
          {block.exercises.map((ex) => (
            <tr
              key={`${ex.name}·${ex.variant}`}
              className="border-b last:border-0"
            >
              <td className="py-2 pr-3">
                {ex.name}
                <span className="ml-1.5 text-xs text-muted-foreground">
                  {ex.variant}
                </span>
              </td>
              <td className="py-2 pr-3 tabular-nums">
                {ex.sets} × {ex.rep_range}
              </td>
              <td className="py-2 pr-3 tabular-nums">{ex.target_rir}</td>
              <td className="py-2 text-xs text-muted-foreground">
                {ex.progression}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** 当前计划卡：版本/状态/日期 + 按板块分组的动作表 */
function PlanCard({ plan }: { plan: PlanVersion }) {
  return (
    <Card className="sm:col-span-2">
      <CardHeader>
        <div className="flex flex-wrap items-center gap-2">
          <CardTitle>当前计划</CardTitle>
          <Badge variant={plan.status === "active" ? "default" : "secondary"}>
            {plan.status === "active" ? "生效中" : "已归档"}
          </Badge>
        </div>
        <CardDescription>
          {plan.version} · 开始 {plan.start_date} · 复核 {plan.review_date}
          ；处方修改须经对话草稿确认并生成新版本
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-6">
        {plan.blocks.map((block) => (
          <BlockTable key={block.name} block={block} />
        ))}
      </CardContent>
    </Card>
  );
}

/** /profile 档案与限制：只读看板，业务变更唯一入口是对话 */
export default function ProfilePage() {
  const profile = useQuery({ queryKey: ["profile"], queryFn: getProfile });

  return (
    <div className="mx-auto w-full max-w-3xl px-6 pb-10">
      <header className="pt-10 pb-6">
        <h2 className="font-display text-3xl font-light tracking-tight">
          档案与限制
        </h2>
        <p className="mt-1 text-sm text-muted-foreground">
          档案、动作限制与当前计划的只读查看
        </p>
      </header>

      {profile.isPending && <Loading text="正在加载档案" />}
      {profile.isError && <LoadError text={profile.error.message} />}

      {profile.data && (
        <>
          <div className="grid gap-4 sm:grid-cols-2">
            <ProfileCard profile={profile.data.profile} />
            <RestrictionsCard restrictions={profile.data.restrictions} />
            {profile.data.plan && <PlanCard plan={profile.data.plan} />}
          </div>

          <footer className="mt-8 flex items-center justify-center gap-1.5 text-sm text-muted-foreground">
            变更请到对话页发起
            <Link
              to="/"
              className="inline-flex items-center gap-1 font-medium text-foreground underline-offset-4 hover:underline"
            >
              前往对话
              <ArrowRight className="size-3.5" aria-hidden />
            </Link>
          </footer>
        </>
      )}
    </div>
  );
}
