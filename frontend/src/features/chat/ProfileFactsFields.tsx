import { Input } from "@/components/ui/input";
import type { Profile } from "@/lib/contract";
import { FieldLine, selectClass, toNumber, withCurrent } from "./draftFields";

/* ------------------------------ 档案草稿字段 ------------------------------ */

/** 六类建档事实 + 必填体重的内联纠错（选择 / 数字 / 文本 / 器械与限制列表增删） */
const PROFILE_GOALS = ["增肌（肌肥大）", "力量", "整体健康"];
const PROFILE_EXPERIENCES = ["零基础", "初级（有少量训练经验）", "中级"];

export function ProfileFactsFields({
  prof,
  disabled,
  patch,
}: {
  prof: Partial<Profile>;
  disabled: boolean;
  patch: (p: Partial<Profile>) => void;
}) {
  return (
    <>
      <FieldLine label="训练目标">
        <select
          aria-label="训练目标"
          className={selectClass}
          value={prof.goal ?? ""}
          disabled={disabled}
          onChange={(e) => patch({ goal: e.target.value })}
        >
          {prof.goal === undefined && <option value="">未收集</option>}
          {withCurrent(PROFILE_GOALS, prof.goal).map((g) => (
            <option key={g} value={g}>
              {g}
            </option>
          ))}
        </select>
      </FieldLine>

      <FieldLine label="训练经验">
        <select
          aria-label="训练经验"
          className={selectClass}
          value={prof.experience ?? ""}
          disabled={disabled}
          onChange={(e) => patch({ experience: e.target.value })}
        >
          {prof.experience === undefined && <option value="">未收集</option>}
          {withCurrent(PROFILE_EXPERIENCES, prof.experience).map((v) => (
            <option key={v} value={v}>
              {v}
            </option>
          ))}
        </select>
      </FieldLine>

      <FieldLine label="每周频率">
        <Input
          type="number"
          min={1}
          value={prof.weekly_frequency ?? ""}
          disabled={disabled}
          onChange={(e) =>
            patch({ weekly_frequency: toNumber(e.target.value) })
          }
          className="h-7 w-20 text-xs"
          aria-label="每周训练频率（次）"
        />
        <span className="text-muted-foreground">次 / 周</span>
      </FieldLine>

      <FieldLine label="单次时长">
        <Input
          type="number"
          min={1}
          value={prof.session_minutes ?? ""}
          disabled={disabled}
          onChange={(e) => patch({ session_minutes: toNumber(e.target.value) })}
          className="h-7 w-20 text-xs"
          aria-label="单次训练时长（分钟）"
        />
        <span className="text-muted-foreground">分钟</span>
      </FieldLine>

      <FieldLine label="体重（必填）">
        <Input
          type="number"
          min={0}
          step="0.5"
          value={prof.body_weight_kg ?? ""}
          disabled={disabled}
          onChange={(e) => patch({ body_weight_kg: toNumber(e.target.value) })}
          className="h-7 w-20 text-xs"
          aria-label="体重（kg）"
        />
        <span className="text-muted-foreground">kg</span>
      </FieldLine>
    </>
  );
}
