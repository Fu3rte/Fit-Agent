/**
 * /plans 计划页（stage5.md §3.10、Subtask 06；stage6.md §2.5.4）：active、唯一 draft（结构化计划与
 * Evaluator 摘要）与 archived／rejected 历史的只读展示。
 *
 * 两条硬边界：
 *
 * - **不重算业务数值**：结构化计划与 Evaluator 结果按后端字段原样展示（只做 JSON 缩进），PB／趋势／
 *   完成率等只展示后端文本，不在前端计算、补算或判断进步退步。
 * - **本页没有任何写入入口**：生成／调整计划与 draft 的确认／拒绝都在对话页（stage6.md §2.5.4），
 *   本页不跑 ``/api/agent/run``、不调 ``/api/agent/confirm``／``reject``，也不写任何训练记录。
 */
import { useQuery } from "@tanstack/react-query";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { getActivePlan, listPlans } from "@/lib/api";
import type { PlanWire } from "@/lib/contract";

/** 计划 Query key：``["plans"]`` 前缀同时覆盖 active 与全部版本列表 */
const ACTIVE_PLAN_KEY = ["plans", "active"];
const PLAN_LIST_KEY = ["plans", "list"];

const PLAN_STATUS_LABELS: Record<PlanWire["status"], string> = {
  draft: "待确认",
  active: "已启用",
  archived: "已归档",
  rejected: "评估未通过",
};

/** 后端 JSON 字段原样展示：只缩进，不解释领域形状，也不派生任何业务字段 */
function JsonBlock({ value }: { value: unknown }) {
  return (
    <pre className="max-h-72 overflow-auto rounded-md border border-border/60 bg-muted/40 p-3 text-xs leading-relaxed">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

/** 一个计划版本的身份与时间字段；不展示结构化内容（由卡片各自决定） */
function PlanMeta({ plan }: { plan: PlanWire }) {
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
      <Badge variant={plan.status === "active" ? "default" : "outline"}>
        {PLAN_STATUS_LABELS[plan.status]}
      </Badge>
      <span className="tabular-nums">#{plan.id}</span>
      <span className="tabular-nums">版本 {plan.version}</span>
      {plan.source_plan_id !== null && (
        <span className="tabular-nums">来源计划 #{plan.source_plan_id}</span>
      )}
      <span className="tabular-nums">创建 {plan.created_at}</span>
      {plan.confirmed_at !== null && (
        <span className="tabular-nums">启用 {plan.confirmed_at}</span>
      )}
      {plan.archived_at !== null && (
        <span className="tabular-nums">归档 {plan.archived_at}</span>
      )}
    </div>
  );
}

/** /plans 计划页：active、待确认 draft 与历史版本的只读展示 */
export default function PlansPage() {
  const active = useQuery({ queryKey: ACTIVE_PLAN_KEY, queryFn: getActivePlan });
  const plans = useQuery({ queryKey: PLAN_LIST_KEY, queryFn: listPlans });

  const versions = plans.data?.plans ?? [];
  const draft = versions.find((plan) => plan.status === "draft") ?? null;
  const history = versions
    .filter((plan) => plan.status === "archived" || plan.status === "rejected")
    .sort((a, b) => b.version - a.version);

  return (
    <div className="mx-auto w-full max-w-3xl px-6 pb-10">
      <header className="pt-10 pb-6">
        <h2 className="font-display text-3xl font-light tracking-tight">
          训练计划
        </h2>
        <p className="mt-1 text-sm text-muted-foreground">
          查看当前启用计划、待确认 draft 与历史版本；生成／调整与确认／拒绝都在「对话」页。
        </p>
      </header>

      <div className="flex flex-col gap-6">
        {active.isPending && (
          <p className="text-sm text-muted-foreground">正在加载当前计划…</p>
        )}
        {active.isError && (
          <p className="text-sm text-destructive">
            加载当前计划失败：{active.error.message}，请刷新重试。
          </p>
        )}

        <Card>
          <CardHeader>
            <CardTitle>当前启用计划</CardTitle>
            <CardDescription>
              同一时刻至多一个 active；确认新计划时它会被归档。
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            {active.data ? (
              active.data.plan === null ? (
                <p className="text-sm text-muted-foreground">
                  还没有启用的计划。
                </p>
              ) : (
                <>
                  <PlanMeta plan={active.data.plan} />
                  <JsonBlock value={active.data.plan.structured_content} />
                </>
              )
            ) : (
              <p className="text-xs text-muted-foreground">暂无数据。</p>
            )}
          </CardContent>
        </Card>

        {plans.isPending && (
          <p className="text-sm text-muted-foreground">正在加载计划版本…</p>
        )}
        {plans.isError && (
          <p className="text-sm text-destructive">
            加载计划版本失败：{plans.error.message}，请刷新重试。
          </p>
        )}

        {draft && (
          <Card>
            <CardHeader>
              <CardTitle>待确认计划</CardTitle>
              <CardDescription>
                确认启用或拒绝归档在「对话」页完成；本页只展示 draft 与 Evaluator 结果。
              </CardDescription>
            </CardHeader>
            <CardContent className="flex flex-col gap-3">
              <PlanMeta plan={draft} />
              <section className="flex flex-col gap-1.5">
                <h4 className="text-sm font-medium">结构化计划</h4>
                <JsonBlock value={draft.structured_content} />
              </section>
              <section className="flex flex-col gap-1.5">
                <h4 className="text-sm font-medium">Evaluator 摘要</h4>
                {draft.evaluator_result === null ? (
                  <p className="text-xs text-muted-foreground">
                    本次 draft 没有评估结果。
                  </p>
                ) : (
                  <JsonBlock value={draft.evaluator_result} />
                )}
              </section>
            </CardContent>
          </Card>
        )}

        <Card>
          <CardHeader>
            <CardTitle>历史版本</CardTitle>
            <CardDescription>
              已归档与评估未通过的版本；rejected 只表示 Evaluator
              二次阻断失败，不是用户拒绝。
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            {history.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                还没有历史版本。
              </p>
            ) : (
              history.map((plan) => (
                <div
                  key={plan.id}
                  className="flex flex-col gap-2 rounded-md border border-border/60 p-3"
                >
                  <PlanMeta plan={plan} />
                  <JsonBlock value={plan.structured_content} />
                </div>
              ))
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
