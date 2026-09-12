import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import type { RawLoad, SetFacts } from "@/lib/contract";
import { toNumber } from "./draftFields";

/**
 * 组事实编辑器（对齐 S3-10 SetFacts）：
 * - set_type 未明确保持 null（不默认 work）；
 * - 不展示/不录入 RIR（已拍 2026-09-12：隐藏且不落库，保持 null）；
 * - load 保留原始 value_text + unit。
 */
export function SetInputs({
  sets,
  disabled,
  onChange,
}: {
  sets: SetFacts[];
  disabled: boolean;
  onChange: (sets: SetFacts[]) => void;
}) {
  const patch = (i: number, p: Partial<SetFacts>) =>
    onChange(sets.map((s, j) => (j === i ? { ...s, ...p } : s)));

  const patchLoad = (i: number, valueText: string) => {
    const cur = sets[i];
    if (!cur) return;
    if (valueText.trim() === "") {
      patch(i, { load: null });
      return;
    }
    const unit: RawLoad["unit"] = cur.load?.unit ?? "kg";
    patch(i, { load: { value_text: valueText, unit } });
  };

  return (
    <div className="space-y-1.5">
      {sets.map((s, i) => (
        <div key={s.set_no} className="flex flex-wrap items-center gap-1.5 text-xs">
          <span className="w-12 text-muted-foreground">第 {s.set_no} 组</span>
          <Badge
            variant={s.set_type === "warmup" ? "secondary" : "outline"}
            className="text-[10px]"
          >
            {s.set_type === "warmup"
              ? "热身"
              : s.set_type === "work"
                ? "工作"
                : "未明确"}
          </Badge>
          {s.assistance === "assisted" && (
            <Badge variant="secondary" className="text-[10px]">
              辅助
            </Badge>
          )}
          <Input
            type="number"
            min={0}
            value={s.load?.value_text ?? ""}
            placeholder="重量"
            disabled={disabled}
            onChange={(e) => patchLoad(i, e.target.value)}
            className="h-7 w-20 text-xs"
            aria-label={`第 ${s.set_no} 组重量`}
          />
          <select
            className="h-7 rounded-md border border-input bg-transparent px-1 text-[10px] disabled:opacity-50"
            value={s.load?.unit ?? "kg"}
            disabled={disabled}
            onChange={(e) => {
              const cur = sets[i];
              if (!cur) return;
              if (!cur.load) return;
              patch(i, {
                load: { ...cur.load, unit: e.target.value as "kg" | "lb" },
              });
            }}
            aria-label={`第 ${s.set_no} 组单位`}
          >
            <option value="kg">kg</option>
            <option value="lb">lb</option>
          </select>
          <span className="text-muted-foreground">×</span>
          <Input
            type="number"
            min={0}
            value={s.reps ?? ""}
            placeholder="次数"
            disabled={disabled}
            onChange={(e) => patch(i, { reps: toNumber(e.target.value) })}
            className="h-7 w-16 text-xs"
            aria-label={`第 ${s.set_no} 组次数`}
          />
          <select
            className="h-7 rounded-md border border-input bg-transparent px-1 text-[10px] disabled:opacity-50"
            value={s.set_type ?? ""}
            disabled={disabled}
            onChange={(e) =>
              patch(i, {
                set_type:
                  e.target.value === ""
                    ? null
                    : (e.target.value as "warmup" | "work"),
              })
            }
            aria-label={`第 ${s.set_no} 组类型`}
          >
            <option value="">未明确</option>
            <option value="work">工作</option>
            <option value="warmup">热身</option>
          </select>
        </div>
      ))}
    </div>
  );
}
