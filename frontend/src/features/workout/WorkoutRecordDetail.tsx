import { ChevronLeft } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { WorkoutRecordWire } from "@/lib/contract";
import { describeWorkoutContent } from "./workoutBrowse";
import WorkoutFields from "./WorkoutFields";

interface WorkoutRecordDetailProps {
  record: WorkoutRecordWire | null;
  fetching: boolean;
  className?: string;
  onBack: () => void;
}

/**
 * 训练记录详情栏：头部突出日期与动作、组数摘要，正文按共享口径完整呈现训练内容；
 * 窄处顶部以「返回列表」回到列表栏，筛选、页码与选中记录保持不变。
 */
export default function WorkoutRecordDetail({
  record,
  fetching,
  className,
  onBack,
}: WorkoutRecordDetailProps) {
  return (
    <section
      aria-label="训练记录详情"
      aria-busy={fetching}
      className={cn("flex min-h-0 flex-col", className)}
    >
      {record !== null && (
        <>
          <div className="shrink-0 px-4 pt-3 @3xl:hidden">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={onBack}
              className="-ml-2"
            >
              <ChevronLeft aria-hidden />
              返回列表
            </Button>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-6 py-6 [scrollbar-width:none]">
            <h2 className="font-display text-4xl leading-none font-light tracking-tight tabular-nums">
              {record.performed_on}
            </h2>
            <p className="mt-2 text-sm tabular-nums text-muted-foreground">
              {describeWorkoutContent(record.content)}
            </p>
            <div className="mt-6">
              <WorkoutFields content={record.content} />
            </div>
          </div>
        </>
      )}
    </section>
  );
}
