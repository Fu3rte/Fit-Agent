import { useEffect, useState, type ChangeEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { UserRound } from "lucide-react";
import { Field, FieldLabel, FieldTitle } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { listExercises, putProfile } from "@/lib/api";
import type { ProfileFactWire, ProfileFactsWire } from "@/lib/contract";

type FieldKey = keyof ProfileFactsWire;

type FieldKind = "text" | "count" | "list";

const FIELDS: Array<{
  key: FieldKey;
  label: string;
  kind: FieldKind;
}> = [
  {
    key: "training_goal",
    label: "训练目标",
    kind: "text",
  },
  {
    key: "current_level",
    label: "当前水平",
    kind: "text",
  },
  {
    key: "weekly_frequency",
    label: "每周可训练次数",
    kind: "count",
  },
  {
    key: "available_equipment",
    label: "可用器械",
    kind: "list",
  },
  {
    key: "explicit_preferences",
    label: "明确偏好",
    kind: "list",
  },
  {
    key: "known_injuries",
    label: "已知伤病",
    kind: "list",
  },
];

const splitList = (text: string) =>
  text
    .split(/[,，\n]/)
    .map((item) => item.trim())
    .filter(Boolean);

export default function ProfileCard({
  profile,
}: {
  profile: ProfileFactsWire | null;
}) {
  const queryClient = useQueryClient();
  const exercises = useQuery({
    queryKey: ["exercises"],
    queryFn: listExercises,
  });

  const [texts, setTexts] = useState<Record<string, string>>({});
  const [forbidden, setForbidden] = useState<string[]>([]);
  const [editing, setEditing] = useState(false);

  useEffect(() => {
    setTexts({
      training_goal: profile?.training_goal.value ?? "",
      weekly_frequency:
        profile?.weekly_frequency.value != null
          ? String(profile.weekly_frequency.value)
          : "",
      current_level: profile?.current_level.value ?? "",
      available_equipment: (profile?.available_equipment.value ?? []).join(
        "\n",
      ),
      explicit_preferences: (profile?.explicit_preferences.value ?? []).join(
        "\n",
      ),
      known_injuries: (profile?.known_injuries.value ?? []).join("\n"),
    });
    setForbidden(profile?.forbidden_exercise_ids.value ?? []);
  }, [profile]);

  const save = useMutation({
    mutationFn: (body: ProfileFactsWire) => putProfile(body),
    onSuccess: (data) => {
      queryClient.setQueryData(["profile"], data);
      toast.success("画像已保存");
      setEditing(false);
    },
    onError: (error) =>
      toast.error(error instanceof Error ? error.message : "画像保存失败"),
  });

  const isEmpty = (value: unknown) =>
    value === null ||
    (typeof value === "string" && value.trim() === "") ||
    (Array.isArray(value) && value.length === 0);

  /** 只读展示：known 取值，unknown 与 denied 按三态区分措辞 */
  const readValue = (key: FieldKey): string => {
    const field = profile?.[key];
    if (field?.state === "denied") return "无";
    if (field?.state !== "known" || field.value === null) return "未填写";
    return Array.isArray(field.value)
      ? field.value.join("、")
      : String(field.value);
  };

  const fact = <T,>(key: FieldKey, value: T | null): ProfileFactWire<T> => {
    if (isEmpty(value)) {
      const state = profile?.[key].state === "denied" ? "denied" : "unknown";
      return { state, value: null };
    }
    return { state: "known", value: value as T };
  };

  const submit = () => {
    const frequency = texts.weekly_frequency?.trim() ?? "";
    const body: ProfileFactsWire = {
      training_goal: fact("training_goal", texts.training_goal?.trim() ?? ""),
      weekly_frequency: fact(
        "weekly_frequency",
        frequency === "" ? null : Number(frequency),
      ),
      available_equipment: fact(
        "available_equipment",
        splitList(texts.available_equipment ?? ""),
      ),
      explicit_preferences: fact(
        "explicit_preferences",
        splitList(texts.explicit_preferences ?? ""),
      ),
      current_level: fact("current_level", texts.current_level?.trim() ?? ""),
      known_injuries: fact(
        "known_injuries",
        splitList(texts.known_injuries ?? ""),
      ),
      forbidden_exercise_ids: fact("forbidden_exercise_ids", forbidden),
    };
    save.mutate(body);
  };

  return (
    <Card className="flex-1">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <UserRound className="size-4" />
          用户画像
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-1 flex-col gap-4">
        <div className="grid gap-x-6 gap-y-4 sm:grid-cols-2">
          {FIELDS.map(({ key, label, kind }) => {
            const bind = {
              id: key,
              value: texts[key] ?? "",
              onChange: (
                event: ChangeEvent<HTMLInputElement | HTMLTextAreaElement>,
              ) => setTexts((prev) => ({ ...prev, [key]: event.target.value })),
            };
            return (
              <Field key={key} className="gap-2">
                {editing ? (
                  <>
                    <FieldLabel htmlFor={key}>{label}</FieldLabel>
                    {kind === "list" ? (
                      <Textarea {...bind} />
                    ) : (
                      <Input
                        {...bind}
                        inputMode={kind === "count" ? "numeric" : undefined}
                        className={kind === "count" ? "w-24" : undefined}
                      />
                    )}
                  </>
                ) : (
                  <>
                    <FieldTitle>{label}</FieldTitle>
                    <p className="text-sm">{readValue(key)}</p>
                  </>
                )}
              </Field>
            );
          })}

          <Field className="gap-2 sm:col-span-2">
            <FieldTitle>禁用动作</FieldTitle>
            <div className="flex flex-wrap items-center gap-2">
              {forbidden.map((id) => (
                <Badge key={id} variant="secondary">
                  {exercises.data?.exercises.find((e) => e.id === id)
                    ?.standard_name_zh ?? id}
                </Badge>
              ))}
            </div>
          </Field>
        </div>

        <div className="mt-auto flex justify-end">
          {editing ? (
            <Button onClick={submit} disabled={save.isPending}>
              保存画像
            </Button>
          ) : (
            <Button onClick={() => setEditing(true)}>修改画像</Button>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
