/**
 * /profile 用户画像：七字段三态事实的读取与整份覆盖更新（Stage 1 子任务 06）。
 *
 * 逐字对齐 ``/api/profile``：每字段三态（未填写 unknown／明确为空 denied／已知 known），
 * 未填写与明确为空必须可分；known 才携带值。整份覆盖提交（PUT 语义）。无 RIR、
 * 无 context_version、无对话更正入口（讨论总结 §7/§8/§13）。
 */
import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { getProfile, listExercises, putProfile } from "@/lib/api";
import type {
  FactState,
  ProfileFactWire,
  ProfileFactsWire,
} from "@/lib/contract";

const FACT_STATE_LABELS: Record<FactState, string> = {
  unknown: "未填写",
  denied: "明确为空",
  known: "已知",
};

type FieldKey =
  | "training_goal"
  | "weekly_frequency"
  | "available_equipment"
  | "explicit_preferences"
  | "current_level"
  | "known_injuries"
  | "forbidden_exercise_ids";

const FIELDS: Array<{ key: FieldKey; label: string; hint: string }> = [
  { key: "training_goal", label: "训练目标", hint: "例：增肌、力量、综合健康" },
  { key: "weekly_frequency", label: "每周可训练次数", hint: "正整数" },
  { key: "available_equipment", label: "可用器械", hint: "多项用逗号分隔" },
  { key: "explicit_preferences", label: "明确偏好", hint: "多项用逗号分隔" },
  { key: "current_level", label: "当前水平", hint: "例：新手、中级" },
  { key: "known_injuries", label: "已知伤病", hint: "多项用逗号分隔" },
  { key: "forbidden_exercise_ids", label: "禁用动作", hint: "从动作目录勾选" },
];

const LIST_KEYS: FieldKey[] = [
  "available_equipment",
  "explicit_preferences",
  "known_injuries",
];

const splitList = (text: string) =>
  text
    .split(/[,，]/)
    .map((item) => item.trim())
    .filter(Boolean);

export default function ProfilePage() {
  const queryClient = useQueryClient();
  const profile = useQuery({ queryKey: ["profile"], queryFn: getProfile });
  const exercises = useQuery({ queryKey: ["exercises"], queryFn: listExercises });

  const [states, setStates] = useState<Record<FieldKey, FactState>>({
    training_goal: "unknown",
    weekly_frequency: "unknown",
    available_equipment: "unknown",
    explicit_preferences: "unknown",
    current_level: "unknown",
    known_injuries: "unknown",
    forbidden_exercise_ids: "unknown",
  });
  const [texts, setTexts] = useState<Record<string, string>>({});
  const [forbidden, setForbidden] = useState<string[]>([]);

  const loaded = profile.data?.profile ?? null;
  useEffect(() => {
    setStates({
      training_goal: loaded?.training_goal.state ?? "unknown",
      weekly_frequency: loaded?.weekly_frequency.state ?? "unknown",
      available_equipment: loaded?.available_equipment.state ?? "unknown",
      explicit_preferences: loaded?.explicit_preferences.state ?? "unknown",
      current_level: loaded?.current_level.state ?? "unknown",
      known_injuries: loaded?.known_injuries.state ?? "unknown",
      forbidden_exercise_ids: loaded?.forbidden_exercise_ids.state ?? "unknown",
    });
    setTexts({
      training_goal: loaded?.training_goal.value ?? "",
      weekly_frequency:
        loaded?.weekly_frequency.value != null
          ? String(loaded.weekly_frequency.value)
          : "",
      current_level: loaded?.current_level.value ?? "",
      available_equipment: (loaded?.available_equipment.value ?? []).join("，"),
      explicit_preferences: (loaded?.explicit_preferences.value ?? []).join("，"),
      known_injuries: (loaded?.known_injuries.value ?? []).join("，"),
    });
    setForbidden(loaded?.forbidden_exercise_ids.value ?? []);
  }, [loaded]);

  const nameById = useMemo(
    () => new Map((exercises.data?.exercises ?? []).map((e) => [e.id, e])),
    [exercises.data],
  );

  const save = useMutation({
    mutationFn: (body: ProfileFactsWire) => putProfile(body),
    onSuccess: (data) => {
      queryClient.setQueryData(["profile"], data);
      toast.success("画像已保存");
    },
    onError: (error) =>
      toast.error(error instanceof Error ? error.message : "画像保存失败"),
  });

  if (profile.isLoading) {
    return <p className="mt-10 text-sm text-muted-foreground">加载画像…</p>;
  }
  if (profile.isError) {
    return (
      <p className="mt-10 text-sm text-destructive">
        加载失败：{profile.error.message}，请刷新重试。
      </p>
    );
  }

  const fact = <T,>(key: FieldKey, value: T): ProfileFactWire<T> => ({
    state: states[key],
    value: states[key] === "known" ? value : null,
  });

  const submit = () => {
    const body: ProfileFactsWire = {
      training_goal: fact("training_goal", texts.training_goal.trim()),
      weekly_frequency: fact(
        "weekly_frequency",
        Number(texts.weekly_frequency),
      ),
      available_equipment: fact(
        "available_equipment",
        splitList(texts.available_equipment ?? ""),
      ),
      explicit_preferences: fact(
        "explicit_preferences",
        splitList(texts.explicit_preferences ?? ""),
      ),
      current_level: fact("current_level", texts.current_level.trim()),
      known_injuries: fact(
        "known_injuries",
        splitList(texts.known_injuries ?? ""),
      ),
      forbidden_exercise_ids: fact("forbidden_exercise_ids", forbidden),
    };
    save.mutate(body);
  };

  const onFactState = (key: FieldKey, state: FactState) =>
    setStates((prev) => ({ ...prev, [key]: state }));

  return (
    <div className="mx-auto max-w-3xl space-y-6 p-6">
      <Card>
        <CardHeader>
          <CardTitle>用户画像</CardTitle>
          <CardDescription>
            七字段三态：未填写与明确为空必须区分，两者都不补造事实；整份覆盖保存。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-5">
          {FIELDS.map(({ key, label, hint }) => (
            <div key={key} className="space-y-2">
              <div className="flex items-center gap-3">
                <span className="w-32 text-sm font-medium">{label}</span>
                <Select
                  value={states[key]}
                  onValueChange={(value) =>
                    onFactState(key, value as FactState)
                  }
                >
                  <SelectTrigger aria-label={`${label}填写状态`}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {(Object.keys(FACT_STATE_LABELS) as FactState[]).map(
                      (s) => (
                        <SelectItem key={s} value={s}>
                          {FACT_STATE_LABELS[s]}
                        </SelectItem>
                      ),
                    )}
                  </SelectContent>
                </Select>
                {states[key] !== "known" && (
                  <span className="text-xs text-muted-foreground">
                    {states[key] === "unknown" ? "尚未填写" : "用户明确表示没有"}
                  </span>
                )}
              </div>
              {states[key] === "known" && key !== "forbidden_exercise_ids" && (
                <Input
                  value={texts[key] ?? ""}
                  placeholder={hint}
                  onChange={(event) =>
                    setTexts((prev) => ({ ...prev, [key]: event.target.value }))
                  }
                />
              )}
              {states[key] === "known" && key === "forbidden_exercise_ids" && (
                <div className="flex flex-wrap gap-2">
                  {(exercises.data?.exercises ?? []).map((exercise) => (
                    <label
                      key={exercise.id}
                      className="flex cursor-pointer items-center gap-1 text-xs"
                    >
                      <input
                        type="checkbox"
                        checked={forbidden.includes(exercise.id)}
                        onChange={(event) =>
                          setForbidden((prev) =>
                            event.target.checked
                              ? [...prev, exercise.id]
                              : prev.filter((id) => id !== exercise.id),
                          )
                        }
                      />
                      {exercise.standard_name_zh}
                    </label>
                  ))}
                </div>
              )}
            </div>
          ))}

          {forbidden.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {forbidden.map((id) => (
                <Badge key={id} variant="secondary">
                  {nameById.get(id)?.standard_name_zh ?? id}
                </Badge>
              ))}
            </div>
          )}

          {LIST_KEYS.some((key) => states[key] === "known") && (
            <p className="text-xs text-muted-foreground">
              列表字段的「已知」不得留空：明确为空请选择「明确为空」。
            </p>
          )}

          <Button onClick={submit} disabled={save.isPending}>
            保存画像
          </Button>
        </CardContent>
      </Card>
    </div>
  );
}
