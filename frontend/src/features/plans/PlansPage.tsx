import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Sheet,
  SheetClose,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import PlanFields from "@/features/plans/PlanFields";
import { getCurrentPlan, getPlan, listPlans } from "@/lib/api";
import { PLAN_QUERY_KEY } from "@/lib/query";
import { formatTimestamp } from "@/lib/utils";

/**
 * 训练计划页（plan-generation-contract §5、§6，PRODUCT.md §3.3）：只读展示当前计划与全部已保存版本；
 * 当前计划查询失败与没有当前计划分别呈现，版本顺序沿用后端返回顺序，是否当前由实时查询给出，
 * 版本完整内容在右侧抽屉内复用计划看板只读呈现。
 * 生成、修改待确认建议与确认保存在对话中经自然语言确认后完成，保存落定使本页查询失效重取。
 */
export default function PlansPage() {
  const [openedPlanId, setOpenedPlanId] = useState<string | null>(null);

  const current = useQuery({
    queryKey: [...PLAN_QUERY_KEY, "current"],
    queryFn: ({ signal }) => getCurrentPlan(signal),
  });
  const versions = useQuery({
    queryKey: [...PLAN_QUERY_KEY, "list"],
    queryFn: ({ signal }) => listPlans(signal),
  });
  const opened = useQuery({
    queryKey: [...PLAN_QUERY_KEY, "record", openedPlanId],
    queryFn: ({ signal }) => getPlan(openedPlanId as string, signal),
    enabled: openedPlanId !== null,
  });
  const openedRecord =
    versions.data?.find((record) => record.id === openedPlanId) ?? opened.data;

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex w-full max-w-4xl flex-col gap-6 px-6 py-10">
        <Card>
          <CardHeader>
            <CardTitle>当前计划</CardTitle>
          </CardHeader>
          <CardContent
            className="flex flex-col gap-4"
            aria-busy={current.isFetching}
          >
            {current.isError && (
              <p role="alert" className="text-sm text-destructive">
                {current.error.message}
              </p>
            )}
            {current.data !== undefined &&
              (current.data.id === null ? (
                <p role="status" className="text-sm">
                  没有当前计划。
                </p>
              ) : (
                <PlanFields content={current.data.content} />
              ))}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-lg font-semibold tracking-tight">
              已保存版本
            </CardTitle>
          </CardHeader>
          <CardContent
            className="flex flex-col gap-4"
            aria-busy={versions.isFetching}
          >
            {versions.isError && (
              <p role="alert" className="text-sm text-destructive">
                {versions.error.message}
              </p>
            )}
            {versions.data !== undefined && versions.data.length === 0 && (
              <p role="status" className="text-sm">
                没有已保存的计划版本。
              </p>
            )}
            {versions.data !== undefined && versions.data.length > 0 && (
              <div className="divide-y divide-border rounded-md border bg-card">
                {versions.data.map((record) => (
                  <div
                    key={record.id}
                    className="flex items-center justify-between gap-4 p-3.5"
                  >
                    <div className="flex items-center gap-2.5">
                      <span className="font-mono text-xs text-foreground tabular-nums">
                        {formatTimestamp(record.created_at)}
                      </span>
                      {record.is_current && (
                        <Badge
                          variant="secondary"
                          className="h-5 px-1.5 text-[10px] font-medium"
                        >
                          当前
                        </Badge>
                      )}
                    </div>
                    <div className="shrink-0">
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        className="h-8 px-3 text-xs"
                        onClick={() => setOpenedPlanId(record.id)}
                      >
                        查看完整内容
                      </Button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      <Sheet
        open={openedPlanId !== null}
        onOpenChange={(open) => {
          if (!open) setOpenedPlanId(null);
        }}
      >
        <SheetContent
          side="right"
          className="flex h-full w-full flex-col gap-0 bg-background p-0 sm:max-w-2xl md:max-w-3xl lg:max-w-4xl"
        >
          <SheetHeader className="shrink-0 flex-col gap-1.5 border-b bg-muted/20 p-5">
            <div className="flex items-center justify-between">
              <SheetTitle className="text-base font-semibold">
                历史版本详情
              </SheetTitle>
              {openedRecord?.is_current && (
                <Badge variant="secondary">当前生效中</Badge>
              )}
            </div>
            {openedRecord !== undefined && (
              <SheetDescription className="font-mono text-xs tabular-nums text-muted-foreground">
                {`生成于 ${formatTimestamp(openedRecord.created_at)}`}
              </SheetDescription>
            )}
          </SheetHeader>
          <div
            className="flex-1 space-y-6 overflow-y-auto p-6"
            aria-busy={opened.isFetching}
          >
            {opened.isError && (
              <p role="alert" className="text-sm text-destructive">
                {opened.error.message}
              </p>
            )}
            {opened.data !== undefined && (
              <PlanFields content={opened.data.content} />
            )}
          </div>
          <SheetFooter className="shrink-0 flex-row justify-end border-t bg-background p-4">
            <SheetClose asChild>
              <Button type="button" variant="outline" size="sm">
                关闭
              </Button>
            </SheetClose>
          </SheetFooter>
        </SheetContent>
      </Sheet>
    </div>
  );
}
