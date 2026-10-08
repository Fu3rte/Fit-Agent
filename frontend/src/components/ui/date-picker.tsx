import { useState } from "react";
import { CalendarIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Calendar } from "@/components/ui/calendar";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { cn, dateFromIso, isoFromDate } from "@/lib/utils";

/**
 * shadcn Date Picker（Popover ＋ Calendar 组合）：值为 ``YYYY-MM-DD``，选中即回写并收起浮层。
 * 触发器与 Input 同规格（h-10、rounded-md、hairline-strong 描边），文案用本地年月日。
 */
function DatePicker({
  value,
  onChange,
  label,
  emptyText,
  className,
}: {
  value: string;
  onChange: (iso: string) => void;
  /** 触发器可访问名（表单里同一页可能有多个日期控件） */
  label: string;
  /** 允许空值的调用方给出的空值文案；省略时值为空不出现，再次点选已选日期也不可取消 */
  emptyText?: string;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const selected = value === "" ? undefined : dateFromIso(value);

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          variant="outline"
          aria-label={label}
          className={cn(
            "w-44 justify-start gap-2 text-left font-normal tabular-nums",
            value === "" && "text-muted-foreground",
            className,
          )}
        >
          <CalendarIcon className="text-muted-foreground" aria-hidden />
          {value === "" ? emptyText : value.replaceAll("-", "/")}
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-auto p-0">
        <Calendar
          mode="single"
          required={emptyText === undefined}
          selected={selected}
          defaultMonth={selected}
          onSelect={(next: Date | undefined) => {
            onChange(next === undefined ? "" : isoFromDate(next));
            setOpen(false);
          }}
        />
      </PopoverContent>
    </Popover>
  );
}

export { DatePicker };
