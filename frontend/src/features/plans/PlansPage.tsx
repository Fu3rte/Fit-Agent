import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import PlanFields from "@/features/plans/PlanFields";
import { getCurrentPlan, getPlan, listPlans } from "@/lib/api";
import { PLAN_QUERY_KEY } from "@/lib/query";
import { formatTimestamp } from "@/lib/utils";

/**
 * 训练计划页（plan-generation-contract §5、§6，PRODUCT.md §3.3）：只读展示当前计划与全部已保存版本；
 * 当前计划查询失败与没有当前计划分别呈现，版本顺序沿用后端返回顺序，是否当前由实时查询给出。
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
            <CardTitle>已保存版本</CardTitle>
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
            {versions.data?.map((record) => (
              <div key={record.id} className="flex flex-col gap-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm tabular-nums">
                    {formatTimestamp(record.created_at)}
                  </span>
                  {record.is_current && <Badge>当前</Badge>}
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    aria-expanded={openedPlanId === record.id}
                    onClick={() =>
                      setOpenedPlanId(openedPlanId === record.id ? null : record.id)
                    }
                  >
                    {openedPlanId === record.id ? "收起" : "查看完整内容"}
                  </Button>
                </div>
                {openedPlanId === record.id && (
                  <div className="flex flex-col gap-4">
                    {opened.isError && (
                      <p role="alert" className="text-sm text-destructive">
                        {opened.error.message}
                      </p>
                    )}
                    {opened.data !== undefined && (
                      <PlanFields content={opened.data.content} />
                    )}
                  </div>
                )}
              </div>
            ))}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
