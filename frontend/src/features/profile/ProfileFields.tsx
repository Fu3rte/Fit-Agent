import { Badge } from "@/components/ui/badge";
import { Field, FieldTitle } from "@/components/ui/field";
import type { ProfileContentWire } from "@/lib/contract";

/** 文本字段与列表字段的取值集合固定，未知状态按字段定义分别措辞（§11.9） */
const TEXT_FIELDS = [
  { key: "goal", label: "训练目标" },
  { key: "experience", label: "训练经验" },
  { key: "environment", label: "训练环境" },
  { key: "availability", label: "可用时间与频率" },
  { key: "health_notes", label: "伤病与不适" },
  { key: "movement_restrictions", label: "动作限制" },
] as const;

const LIST_FIELDS = [
  {
    key: "unavailable_equipment",
    label: "不可用器械",
    unknown: "默认动作目录中的全部器械可用",
  },
  { key: "forbidden_exercise_ids", label: "禁用动作", unknown: "未知" },
] as const;

/**
 * 画像八个字段的展示：null 保持未知语义，限制列表的 null 与 [] 分别展示（§11.9）；
 * 个人画像页与对话中的画像完整展示共用同一口径。
 */
export default function ProfileFields({
  content,
}: {
  content: ProfileContentWire;
}) {
  return (
    <div className="grid gap-x-6 gap-y-4 sm:grid-cols-2">
      {TEXT_FIELDS.map(({ key, label }) => (
        <Field key={key} className="gap-2">
          <FieldTitle>{label}</FieldTitle>
          <p className="text-sm whitespace-pre-wrap">
            {content[key] ?? "未知"}
          </p>
        </Field>
      ))}
      {LIST_FIELDS.map(({ key, label, unknown }) => {
        const items = content[key];
        return (
          <Field key={key} className="gap-2 sm:col-span-2">
            <FieldTitle>{label}</FieldTitle>
            {items === null ? (
              <p className="text-sm">{unknown}</p>
            ) : items.length === 0 ? (
              <p className="text-sm">无</p>
            ) : (
              <div className="flex flex-wrap gap-2">
                {items.map((item) => (
                  <Badge key={item} variant="secondary">
                    {item}
                  </Badge>
                ))}
              </div>
            )}
          </Field>
        );
      })}
    </div>
  );
}
