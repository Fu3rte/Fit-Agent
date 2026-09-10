import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ArrowRight, MessagesSquare, ShieldAlert } from "lucide-react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { buttonVariants } from "@/components/ui/button";
import { getProfile } from "@/lib/api";
import { cn } from "@/lib/utils";
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

interface ProfileRow {
  label: string;
  value: string;
  /** 红旗事实用警示色强调；其余字段为普通正文 */
  alert?: boolean;
}

/** 档案卡：六类事实（目标/经验/频率/时长/器械/体重/当前身体状态，PRD 5.2，只读） */
function ProfileCard({ profile }: { profile: Profile }) {
  const ps = profile.physical_state;
  const rows: ProfileRow[] = [
    { label: "训练目标", value: profile.goal },
    { label: "训练经验", value: profile.experience },
    { label: "每周频率", value: `${profile.weekly_frequency} 次 / 周` },
    { label: "单次时长", value: `${profile.session_minutes} 分钟` },
    {
      label: "可用器械",
      value: profile.equipment.length > 0 ? profile.equipment.join("、") : "无",
    },
    { label: "体重", value: `${profile.body_weight_kg} kg` },
    {
      label: "当前身体状态 · 红旗症状",
      value: ps.red_flags.length > 0 ? ps.red_flags.join("、") : "无明确红旗",
      alert: ps.red_flags.length > 0,
    },
    {
      label: "当前身体状态 · 其他",
      value: ps.notes.length > 0 ? ps.notes.join("、") : "无",
    },
  ];
  return (
    <Card>
      <CardHeader>
        <CardTitle>档案</CardTitle>
        <CardDescription>已确认的训练档案事实</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2.5 text-sm">
        {rows.map((row) => (
          <div
            key={row.label}
            className="flex items-baseline justify-between gap-4"
          >
            <span className="shrink-0 text-muted-foreground">{row.label}</span>
            <span
              className={cn(
                "text-right font-medium",
                row.alert && "text-destructive",
              )}
            >
              {row.value}
            </span>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

/** 动作限制卡：当前有效限制一律红色「暂禁」徽章（展示文案，不代表新增限制状态语义） */
function RestrictionsCard({ restrictions }: { restrictions: Restriction[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>动作限制</CardTitle>
        <CardDescription>
          红色「暂禁」仅为展示文案；限制经确认长期保留，增删须在对话页经草稿确认
        </CardDescription>
      </CardHeader>
      <CardContent>
        {restrictions.length === 0 ? (
          <p className="text-sm text-muted-foreground">暂无动作限制</p>
        ) : (
          <ul className="flex flex-wrap gap-2">
            {restrictions.map((r) => (
              <li key={r.name}>
                <Badge
                  variant="destructive"
                  title={r.note}
                  className="gap-1 py-1"
                >
                  <ShieldAlert className="size-3" aria-hidden />
                  {r.name}
                  {" · "}
                  {r.scope === "specific_action" ? "具体动作" : "动作模式"}
                  {" · 暂禁"}
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
      {/* table-fixed + 固定列宽：三个板块是三张独立表，自动布局会按各自内容算列宽，导致跨表不对齐 */}
      <table className="w-full table-fixed text-sm">
        <thead>
          <tr className="border-b text-left text-xs text-muted-foreground">
            <th className="w-[40%] py-1.5 pr-3 font-normal">动作</th>
            <th className="w-[18%] py-1.5 pr-3 font-normal">组 × 次</th>
            <th className="w-[14%] py-1.5 pr-3 font-normal">目标 RIR</th>
            <th className="w-[28%] py-1.5 font-normal">渐进方式</th>
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

      {/* 未建档：契约 profile = null；对话是唯一建档入口，本页无任何编辑入口（PRD §5.2） */}
      {profile.data && profile.data.profile === null && (
        <Card className="mt-6">
          <CardHeader>
            <div className="flex items-center gap-2">
              <MessagesSquare
                className="size-5 shrink-0 text-muted-foreground"
                aria-hidden
              />
              <CardTitle>尚未建档</CardTitle>
            </div>
            <CardDescription>
              建档只能通过对话完成，没有独立表单；信息齐备后生成档案草稿，确认后在此查看档案与限制。
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Link to="/" className={buttonVariants({ size: "sm" })}>
              前往对话开始建档
              <ArrowRight aria-hidden />
            </Link>
          </CardContent>
        </Card>
      )}

      {profile.data && profile.data.profile && (
        <>
          <div className="grid gap-4 sm:grid-cols-2">
            <ProfileCard profile={profile.data.profile} />
            <RestrictionsCard restrictions={profile.data.restrictions} />
            {profile.data.plan ? (
              <PlanCard plan={profile.data.plan} />
            ) : (
              <Card className="sm:col-span-2">
                <CardHeader>
                  <CardTitle>当前计划</CardTitle>
                </CardHeader>
                <CardContent>
                  <p className="text-sm text-muted-foreground">尚无计划</p>
                </CardContent>
              </Card>
            )}
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
