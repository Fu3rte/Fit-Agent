import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import type { RecordSet } from "@/lib/contract";
import { toNumber } from "./draftFields";

export function SetInputs({
  sets,
  disabled,
  onChange,
}: {
  sets: RecordSet[];
  disabled: boolean;
  onChange: (sets: RecordSet[]) => void;
}) {
  const patch = (i: number, p: Partial<RecordSet>) =>
    onChange(sets.map((s, j) => (j === i ? { ...s, ...p } : s)));
  return (
    <div className="space-y-1.5">
      {sets.map((s, i) => (
        <div key={i} className="flex flex-wrap items-center gap-1.5 text-xs">
          <span className="w-12 text-muted-foreground">第 {i + 1} 组</span>
          <Badge
            variant={s.set_type === "warmup" ? "secondary" : "outline"}
            className="text-[10px]"
          >
            {s.set_type === "warmup" ? "热身" : "工作"}
          </Badge>
          {s.assisted && (
            <Badge variant="secondary" className="text-[10px]">
              辅助
            </Badge>
          )}
          <Input
            type="number"
            min={0}
            value={s.weight_kg ?? ""}
            placeholder="重量"
            disabled={disabled}
            onChange={(e) => patch(i, { weight_kg: toNumber(e.target.value) })}
            className="h-7 w-20 text-xs"
            aria-label={`第 ${i + 1} 组重量（kg）`}
          />
          <span className="text-muted-foreground">kg ×</span>
          <Input
            type="number"
            min={0}
            value={s.reps ?? ""}
            placeholder="次数"
            disabled={disabled}
            onChange={(e) => patch(i, { reps: toNumber(e.target.value) })}
            className="h-7 w-16 text-xs"
            aria-label={`第 ${i + 1} 组次数`}
          />
          <span className="text-muted-foreground">次 · RIR</span>
          <Input
            type="number"
            min={0}
            step="0.5"
            value={s.rir ?? ""}
            placeholder="未报告"
            disabled={disabled}
            onChange={(e) => patch(i, { rir: toNumber(e.target.value) })}
            className="h-7 w-16 text-xs"
            aria-label={`第 ${i + 1} 组 RIR`}
          />
        </div>
      ))}
    </div>
  );
}
