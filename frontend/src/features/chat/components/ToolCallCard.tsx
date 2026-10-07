import { CheckCircle2, ChevronDown, Loader2, XCircle } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { ScrollArea } from "@/components/ui/scroll-area";

export type ToolCallCardProps = {
  name: string;
  arguments: Record<string, unknown>;
  status: "running" | "completed" | "failed" | "cancelled";
  content?: string;
  error?: string;
};

const statuses = {
  running: { label: "执行中", icon: Loader2, className: "animate-spin" },
  completed: { label: "已完成", icon: CheckCircle2, className: "text-primary" },
  failed: { label: "失败", icon: XCircle, className: "text-destructive" },
  cancelled: {
    label: "已中断",
    icon: XCircle,
    className: "text-muted-foreground",
  },
};

export default function ToolCallCard({
  name,
  arguments: args,
  status,
  content,
  error,
}: ToolCallCardProps) {
  const state = statuses[status];
  const Icon = state.icon;
  const summary = Object.entries(args)
    .filter(([key]) =>
      ["path", "pattern", "glob", "offset", "limit"].includes(key),
    )
    .map(([key, value]) => `${key}: ${JSON.stringify(value)}`)
    .join(" · ");

  return (
    <Collapsible asChild defaultOpen={false}>
      <Card className="min-w-0 gap-3 p-4 text-sm">
        <div className="flex min-w-0 items-center gap-2">
          <Icon
            aria-hidden="true"
            className={`size-4 shrink-0 ${state.className}`}
          />
          <span className="min-w-0 flex-1 break-all font-medium">{name}</span>
          <Badge variant="outline" role="status">
            {state.label}
          </Badge>
          <CollapsibleTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              aria-label={`${name} 工具调用详情`}
              className="group shrink-0"
            >
              详情
              <ChevronDown
                aria-hidden="true"
                className="transition-transform group-data-[state=open]:rotate-180"
              />
            </Button>
          </CollapsibleTrigger>
        </div>
        {summary && (
          <p className="line-clamp-2 break-all text-xs text-muted-foreground">
            {summary}
          </p>
        )}
        <p className="line-clamp-2 whitespace-pre-wrap break-all text-xs text-muted-foreground">
          {error !== undefined
            ? error
            : content !== undefined
              ? content === ""
                ? "工具返回空文本"
                : content
              : status === "running"
                ? "等待工具返回"
                : "未返回结果"}
        </p>
        <CollapsibleContent className="space-y-3 border-t pt-3">
          <section className="min-w-0 space-y-2" aria-label="工具参数">
            <h3 className="text-xs font-medium">参数</h3>
            <ScrollArea
              className="h-48 rounded-md border bg-muted/30"
              aria-label="完整工具参数"
            >
              <pre className="whitespace-pre-wrap break-all p-3 pr-5 text-xs">
                {JSON.stringify(args, null, 2)}
              </pre>
            </ScrollArea>
          </section>
          <section className="min-w-0 space-y-2" aria-label="工具结果">
            <h3 className="text-xs font-medium">原始结果</h3>
            {content !== undefined ? (
              <ScrollArea
                className="h-48 rounded-md border bg-muted/30"
                aria-label="完整工具结果"
              >
                <pre className="whitespace-pre-wrap break-all p-3 pr-5 text-xs">
                  {content === "" ? "工具返回空文本" : content}
                </pre>
              </ScrollArea>
            ) : (
              <p className="text-xs text-muted-foreground">
                {status === "running" ? "等待工具返回" : "未返回结果"}
              </p>
            )}
            {error !== undefined && (
              <p
                className="whitespace-pre-wrap break-all text-xs text-destructive"
                role="alert"
              >
                {error}
              </p>
            )}
          </section>
        </CollapsibleContent>
      </Card>
    </Collapsible>
  );
}
