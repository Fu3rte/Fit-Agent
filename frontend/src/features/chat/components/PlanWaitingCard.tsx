import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

/** 计划路径待确认：确认启用或拒绝归档（详情在「训练计划」页） */
export default function PlanWaitingCard({
  planId,
  busy,
  onConfirm,
  onReject,
}: {
  planId: number;
  busy: boolean;
  onConfirm: () => void;
  onReject: () => void;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>待确认计划 #{planId}</CardTitle>
        <CardDescription>
          确认后启用为新 active；拒绝会归档该
          draft，原计划保持不变。计划详情在「训练计划」页查看。
        </CardDescription>
      </CardHeader>
      <CardContent className="flex justify-end gap-2">
        <Button onClick={onConfirm} disabled={busy}>
          确认启用
        </Button>
        <Button variant="outline" onClick={onReject} disabled={busy}>
          拒绝
        </Button>
      </CardContent>
    </Card>
  );
}
