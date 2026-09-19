import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import type { PersonalBestWire, RecordWire } from "@/lib/contract";

/** 确认写入结果：落库训练事实与后端重查的 PB，本页只展示不重算 */
export default function ConfirmedCard({
  session,
  bests,
}: {
  session: RecordWire;
  bests: PersonalBestWire[];
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>已写入的训练</CardTitle>
        <CardDescription>写入结果与 PB 都由后端给出。</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2 text-sm">
        <div className="flex flex-wrap items-center gap-2">
          <span className="tabular-nums">{session.performed_on}</span>
          <Badge variant="secondary">
            {session.plan_session_id === null
              ? "额外训练"
              : `计划日程 #${session.plan_session_id}`}
          </Badge>
          <span className="text-xs text-muted-foreground">
            {session.sets.length} 组
          </span>
        </div>
        {bests.length === 0 ? (
          <p className="text-xs text-muted-foreground">本次写入没有刷新 PB。</p>
        ) : (
          <ul className="flex flex-col gap-1">
            {bests.map((best) => (
              <li
                key={`${best.exercise_id}-${best.pb_type}`}
                className="tabular-nums"
              >
                {best.exercise_name} · {best.pb_type} · {best.value} ·{" "}
                {best.performed_on} · 第 {best.set_no} 组
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
