import { useMemo, useState } from "react";
import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
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
  createBodyMetric,
  createRecord,
  deleteBodyMetric,
  deleteRecord,
  listBodyMetrics,
  listExercises,
  listPlanSessionCandidates,
  listRecords,
  updateBodyMetric,
  updateRecord,
} from "@/lib/api";
import type {
  BodyMetricWire,
  CatalogRecordType,
  ExerciseWire,
  LoadConvention,
  RecordWire,
  SetTypeWire,
  WorkoutSetInputWire,
} from "@/lib/contract";

/** 组类型固定三态（与后端 workout_sets CHECK 同集合；没有「未申报」态） */
const SET_TYPE_LABELS: Record<SetTypeWire, string> = {
  work: "工作",
  warmup: "热身",
  assisted: "辅助",
};

const LOAD_CONVENTION_LABELS: Record<LoadConvention, string> = {
  barbell_includes_bar_total: "杠铃含杠总重",
  dumbbell_per_hand: "哑铃每手重量",
  machine_pin_displayed_value: "器械插销显示值",
  plate_loaded_total_excluding_empty: "挂片总重（不含空杆）",
  unilateral_setting_per_side: "单侧设置重量",
  external_added_weight: "外加重量（不含体重）",
};

const RECORD_TYPE_LABELS: Record<CatalogRecordType, string> = {
  reps_weight: "负重次数",
  reps_bodyweight: "自重次数",
  time: "计时",
};

/** 表单控件样式：与 components/ui/input 同规格的原生 select */
const selectClass =
  "h-10 rounded-md border border-input bg-transparent px-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40";

/** 客户端本地自然日（表单默认值；业务日期一律由服务端按业务时区判定） */
function todayIso(): string {
  const now = new Date();
  const month = `${now.getMonth() + 1}`.padStart(2, "0");
  const day = `${now.getDate()}`.padStart(2, "0");
  return `${now.getFullYear()}-${month}-${day}`;
}

interface SetRow {
  exerciseId: string;
  setType: SetTypeWire;
  reps: string;
  weight: string;
  /** 计时动作的秒数；非计时动作必须为空（提交时送 null，不送 0） */
  duration: string;
}

function emptySetRow(): SetRow {
  return {
    exerciseId: "",
    setType: "work",
    reps: "",
    weight: "",
    duration: "",
  };
}

/** 既有训练的组 → 表单行：按目录记录口径只回填适用字段，其余字段留空（不保留不适用的旧值） */
function setRowsFromRecord(record: RecordWire): SetRow[] {
  return record.sets.map((set) => ({
    exerciseId: set.exercise_id,
    setType: set.set_type,
    reps: set.reps === null ? "" : String(set.reps),
    weight: set.weight_kg === null ? "" : String(set.weight_kg),
    duration: set.duration_seconds === null ? "" : String(set.duration_seconds),
  }));
}

/**
 * 表单行 → 提交事实：按所选动作的目录记录口径只送适用字段（外加重量：口径 + 重量 + 次数；
 * 纯自重：次数；计时：秒数）；不适用的字段一律送 null，与目录不符的口径在前端不可选，
 * 后端仍会复验（含时长不小于 1 秒的领域唯一规则）。
 */
function toSetInputs(
  rows: SetRow[],
  catalogue: Map<string, ExerciseWire>,
): WorkoutSetInputWire[] {
  if (rows.length === 0) throw new Error("至少需要一组");
  return rows.map((row, index) => {
    const position = index + 1;
    const exercise = catalogue.get(row.exerciseId);
    if (exercise === undefined) {
      throw new Error(`第 ${position} 组：请先选择动作`);
    }
    if (exercise.record_type === "time") {
      const duration = Number(row.duration);
      if (row.duration.trim() === "" || !Number.isInteger(duration) || duration < 1) {
        throw new Error(`第 ${position} 组：计时动作请填写不小于 1 秒的整数秒数`);
      }
      return {
        exercise_id: exercise.id,
        set_type: row.setType,
        reps: null,
        load_convention: null,
        weight_kg: null,
        duration_seconds: duration,
      };
    }
    const reps = Number(row.reps);
    if (row.reps.trim() === "" || !Number.isInteger(reps)) {
      throw new Error(`第 ${position} 组：次数必须是整数`);
    }
    if (exercise.load_convention === null) {
      return {
        exercise_id: exercise.id,
        set_type: row.setType,
        reps,
        load_convention: null,
        weight_kg: null,
        duration_seconds: null,
      };
    }
    const weight = Number.parseFloat(row.weight);
    if (row.weight.trim() === "" || Number.isNaN(weight)) {
      throw new Error(`第 ${position} 组：请填写重量`);
    }
    return {
      exercise_id: exercise.id,
      set_type: row.setType,
      reps,
      load_convention: exercise.load_convention,
      weight_kg: weight,
      duration_seconds: null,
    };
  });
}

/**
 * 训练或身体数据写入后失效记录派生 Query（训练记录、PB、趋势、月历、计划日程状态）。
 * 看板的统计 key（``personal-bests``／``trends``／``calendar``，见 DashboardPage）同样被全量失效覆盖，
 * 因此这里不逐条枚举 key：枚举会在看板命名变化时静默失效，而全量失效对单用户本地库无成本问题。
 */
function invalidateRecordDerivedQueries(queryClient: QueryClient): Promise<void> {
  return queryClient.invalidateQueries();
}

/** 训练记录新增／编辑表单；record 为 null 即新增 */
function RecordFormCard({
  record,
  exercises,
  onDone,
}: {
  record: RecordWire | null;
  exercises: ExerciseWire[];
  onDone: () => void;
}) {
  const queryClient = useQueryClient();
  const catalogue = useMemo(
    () => new Map(exercises.map((exercise) => [exercise.id, exercise])),
    [exercises],
  );
  const [performedOn, setPerformedOn] = useState(
    record?.performed_on ?? todayIso(),
  );
  /* auto = 未手动选择（当天恰有一个候选时关联它）；extra = 明确额外训练；number = 显式日程 */
  const [sessionChoice, setSessionChoice] = useState<"auto" | "extra" | number>(
    record === null ? "auto" : (record.plan_session_id ?? "extra"),
  );
  const [rows, setRows] = useState<SetRow[]>(() =>
    record === null ? [emptySetRow()] : setRowsFromRecord(record),
  );

  const candidates = useQuery({
    queryKey: ["plan-session-candidates", performedOn],
    queryFn: () => listPlanSessionCandidates(performedOn),
    enabled: performedOn !== "",
  });
  const available = candidates.data?.sessions ?? [];

  /* 编辑既有记录时它自己关联的日程已不在候选里（已被占用），但仍必须是可选值。 */
  const options = useMemo(() => {
    const items = available.map((session) => ({
      id: session.id,
      label: `计划 ${session.plan_id} · ${session.scheduled_on}`,
    }));
    const current = record?.plan_session_id ?? null;
    if (current !== null && !items.some((item) => item.id === current)) {
      items.push({ id: current, label: `#${current}（当前关联的日程）` });
    }
    return items;
  }, [available, record?.plan_session_id]);

  const selected: "extra" | number =
    sessionChoice === "auto"
      ? available.length === 1
        ? available[0].id
        : "extra"
      : sessionChoice === "extra" ||
          options.some((option) => option.id === sessionChoice)
        ? sessionChoice
        : "extra";

  const save = useMutation({
    mutationFn: () => {
      const body = {
        performed_on: performedOn,
        plan_session_id: selected === "extra" ? null : selected,
        sets: toSetInputs(rows, catalogue),
      };
      return record === null ? createRecord(body) : updateRecord(record.id, body);
    },
    onSuccess: async () => {
      toast.success(record === null ? "训练记录已保存" : "训练记录已更新");
      await invalidateRecordDerivedQueries(queryClient);
      onDone();
    },
    onError: (error) =>
      toast.error(error instanceof Error ? error.message : "训练记录保存失败"),
  });

  const updateRow = (index: number, patch: Partial<SetRow>) => {
    setRows((current) =>
      current.map((row, position) =>
        position === index ? { ...row, ...patch } : row,
      ),
    );
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>{record === null ? "新增训练记录" : "编辑训练记录"}</CardTitle>
        <CardDescription>
          组序号由服务端按提交顺序分配；负重口径按所选动作目录派生。
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1 text-sm">
            日期
            <Input
              type="date"
              value={performedOn}
              onChange={(event) => setPerformedOn(event.target.value)}
              className="w-44"
            />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            计划日程
            <select
              className={selectClass}
              value={String(selected)}
              onChange={(event) => {
                const value = event.target.value;
                setSessionChoice(value === "extra" ? "extra" : Number(value));
              }}
            >
              <option value="extra">额外训练（不关联计划日程）</option>
              {options.map((option) => (
                <option key={option.id} value={option.id}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
        </div>

        {candidates.isPending && (
          <p className="text-xs text-muted-foreground">正在查询当天计划日程…</p>
        )}
        {candidates.isError && (
          <p className="text-xs text-destructive">
            计划日程候选加载失败：{candidates.error.message}
          </p>
        )}
        {candidates.isSuccess && available.length === 0 && (
          <p className="text-xs text-muted-foreground">
            当天没有可关联的计划日程；保存后按「额外训练」记录。
          </p>
        )}
        {candidates.isSuccess && available.length === 1 && (
          <p className="text-xs text-muted-foreground">
            当天恰有一个计划日程，默认关联它；也可以改为额外训练。
          </p>
        )}
        {candidates.isSuccess && available.length > 1 && (
          <p className="text-xs text-muted-foreground">
            当天有 {available.length} 个计划日程候选，请选择要完成的那个，或明确选择额外训练。
          </p>
        )}

        <div className="flex flex-col gap-3">
          {rows.map((row, index) => {
            const exercise = catalogue.get(row.exerciseId);
            const convention = exercise?.load_convention ?? null;
            const recordType = exercise?.record_type ?? null;
            return (
              <div
                key={index}
                className="flex flex-col gap-1 rounded-md border border-border/60 p-3"
              >
                <div className="flex flex-wrap items-end gap-2">
                  <label className="flex flex-col gap-1 text-xs text-muted-foreground">
                    动作
                    <select
                      className={selectClass}
                      value={row.exerciseId}
                      onChange={(event) =>
                        updateRow(index, {
                          exerciseId: event.target.value,
                          /* 切换动作后清除不再适用的旧值：重量、次数与秒数都不跨动作保留 */
                          reps: "",
                          weight: "",
                          duration: "",
                        })
                      }
                    >
                      <option value="">请选择动作</option>
                      {exercises.map((item) => (
                        <option key={item.id} value={item.id}>
                          {item.standard_name_zh}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="flex flex-col gap-1 text-xs text-muted-foreground">
                    组类型
                    <select
                      className={selectClass}
                      value={row.setType}
                      onChange={(event) =>
                        updateRow(index, {
                          setType: event.target.value as SetTypeWire,
                        })
                      }
                    >
                      {(
                        Object.keys(SET_TYPE_LABELS) as SetTypeWire[]
                      ).map((value) => (
                        <option key={value} value={value}>
                          {SET_TYPE_LABELS[value]}
                        </option>
                      ))}
                    </select>
                  </label>
                  {recordType === "time" ? (
                    <label className="flex flex-col gap-1 text-xs text-muted-foreground">
                      秒数
                      <Input
                        type="number"
                        min={1}
                        step={1}
                        value={row.duration}
                        onChange={(event) =>
                          updateRow(index, { duration: event.target.value })
                        }
                        className="w-28"
                      />
                    </label>
                  ) : (
                    <label className="flex flex-col gap-1 text-xs text-muted-foreground">
                      次数
                      <Input
                        type="number"
                        min={1}
                        max={100}
                        value={row.reps}
                        onChange={(event) =>
                          updateRow(index, { reps: event.target.value })
                        }
                        className="w-24"
                      />
                    </label>
                  )}
                  {convention !== null && (
                    <label className="flex flex-col gap-1 text-xs text-muted-foreground">
                      重量（kg）
                      <Input
                        type="number"
                        min={0}
                        step={0.1}
                        value={row.weight}
                        onChange={(event) =>
                          updateRow(index, { weight: event.target.value })
                        }
                        className="w-28"
                      />
                    </label>
                  )}
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() =>
                      setRows((current) =>
                        current.filter((_, position) => position !== index),
                      )
                    }
                    disabled={rows.length === 1}
                  >
                    <Trash2 aria-hidden />
                    删除组
                  </Button>
                </div>
                {exercise !== undefined && (
                  <p className="text-xs text-muted-foreground">
                    {recordType === "time"
                      ? `${RECORD_TYPE_LABELS.time}型动作：只记录秒数，不记录负重口径、重量与次数`
                      : convention === null
                        ? `${RECORD_TYPE_LABELS[exercise.record_type]}型动作：只记录次数，不记录负重口径与重量`
                        : `负重口径：${LOAD_CONVENTION_LABELS[convention]}${
                            exercise.min_load_increment_kg === null
                              ? ""
                              : ` · 最小加重 ${exercise.min_load_increment_kg}kg`
                          }`}
                  </p>
                )}
              </div>
            );
          })}
          <div>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setRows((current) => [...current, emptySetRow()])}
            >
              <Plus aria-hidden />
              添加组
            </Button>
          </div>
        </div>

        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onDone} disabled={save.isPending}>
            取消
          </Button>
          <Button onClick={() => save.mutate()} disabled={save.isPending}>
            {record === null ? "保存训练记录" : "保存修改"}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

/** 身体指标新增／编辑表单；metric 为 null 即新增 */
function BodyMetricFormCard({
  metric,
  onDone,
}: {
  metric: BodyMetricWire | null;
  onDone: () => void;
}) {
  const queryClient = useQueryClient();
  const [measuredOn, setMeasuredOn] = useState(
    metric?.measured_on ?? todayIso(),
  );
  const [weight, setWeight] = useState(
    metric === null ? "" : String(metric.weight_kg),
  );
  const [bodyFat, setBodyFat] = useState(
    metric === null || metric.body_fat_pct === null
      ? ""
      : String(metric.body_fat_pct),
  );

  const save = useMutation({
    mutationFn: () => {
      const weightKg = Number.parseFloat(weight);
      if (weight.trim() === "" || Number.isNaN(weightKg)) {
        throw new Error("请填写体重");
      }
      const body = {
        measured_on: measuredOn,
        weight_kg: weightKg,
        /* 留空即「不记录体脂」：显式送 null，不补 0。 */
        body_fat_pct:
          bodyFat.trim() === "" ? null : Number.parseFloat(bodyFat),
      };
      if (body.body_fat_pct !== null && Number.isNaN(body.body_fat_pct)) {
        throw new Error("体脂必须是数值，留空即不记录");
      }
      return metric === null
        ? createBodyMetric(body)
        : updateBodyMetric(metric.id, body);
    },
    onSuccess: async () => {
      toast.success(metric === null ? "身体指标已保存" : "身体指标已更新");
      await invalidateRecordDerivedQueries(queryClient);
      onDone();
    },
    onError: (error) =>
      toast.error(error instanceof Error ? error.message : "身体指标保存失败"),
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle>
          {metric === null ? "新增身体指标" : "编辑身体指标"}
        </CardTitle>
        <CardDescription>体重必填；体脂留空即不记录，不补 0。</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1 text-sm">
            日期
            <Input
              type="date"
              value={measuredOn}
              onChange={(event) => setMeasuredOn(event.target.value)}
              className="w-44"
            />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            体重（kg）
            <Input
              type="number"
              min={20}
              max={400}
              step={0.1}
              value={weight}
              onChange={(event) => setWeight(event.target.value)}
              className="w-32"
            />
          </label>
          <label className="flex flex-col gap-1 text-sm">
            体脂（%，可选）
            <Input
              type="number"
              min={0}
              max={100}
              step={0.1}
              value={bodyFat}
              onChange={(event) => setBodyFat(event.target.value)}
              placeholder="留空即不记录"
              className="w-32"
            />
          </label>
        </div>
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onDone} disabled={save.isPending}>
            取消
          </Button>
          <Button onClick={() => save.mutate()} disabled={save.isPending}>
            {metric === null ? "保存身体指标" : "保存修改"}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

/** 一次训练的可读摘要行 */
function RecordCard({
  record,
  nameOf,
  onEdit,
  onDelete,
}: {
  record: RecordWire;
  nameOf: (exerciseId: string) => string;
  onEdit: () => void;
  onDelete: () => Promise<void>;
}) {
  const [confirming, setConfirming] = useState(false);
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-lg leading-none tracking-tight tabular-nums">
            {record.performed_on}
          </span>
          <Badge variant="secondary">
            {record.plan_session_id === null
              ? "额外训练"
              : `计划日程 #${record.plan_session_id}`}
          </Badge>
          <span className="text-xs text-muted-foreground">
            {record.sets.length} 组
          </span>
        </div>
        <CardDescription>训练与组事实，可编辑或删除。</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <ul className="flex flex-col gap-1 text-sm">
          {record.sets.map((set) => (
            <li key={`${set.exercise_id}-${set.set_no}`} className="flex gap-2">
              <span className="text-muted-foreground">
                {nameOf(set.exercise_id)} · 第 {set.set_no} 组 ·{" "}
                {SET_TYPE_LABELS[set.set_type]}
              </span>
              <span className="tabular-nums">
                {set.duration_seconds === null
                  ? `${set.weight_kg === null ? "无负重" : `${set.weight_kg}kg`} · ${set.reps} 次`
                  : `${set.duration_seconds} 秒`}
                {set.load_convention === null
                  ? ""
                  : ` · ${LOAD_CONVENTION_LABELS[set.load_convention]}`}
              </span>
            </li>
          ))}
        </ul>
        <div className="flex justify-end gap-2">
          <Button variant="outline" size="sm" onClick={onEdit}>
            编辑
          </Button>
          <Button
            variant={confirming ? "destructive" : "outline"}
            size="sm"
            onBlur={() => setConfirming(false)}
            onClick={() => {
              if (!confirming) {
                setConfirming(true);
                return;
              }
              void onDelete();
            }}
          >
            {confirming ? "确认删除？" : "删除"}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

/** /records 训练记录与身体指标：表单新增、修改和删除（05；无对话更正／修订链／RIR） */
export default function RecordsPage() {
  const queryClient = useQueryClient();
  const exercises = useQuery({ queryKey: ["exercises"], queryFn: listExercises });
  const records = useQuery({ queryKey: ["records"], queryFn: listRecords });
  const metrics = useQuery({ queryKey: ["body-metrics"], queryFn: listBodyMetrics });

  /* null = 表单未展开；"new" = 新增；number 已被下面的 recordForm 表达 */
  const [recordForm, setRecordForm] = useState<"new" | number | null>(null);
  const [metricForm, setMetricForm] = useState<"new" | number | null>(null);

  const catalogue = exercises.data?.exercises ?? [];
  const nameOf = (exerciseId: string) =>
    catalogue.find((exercise) => exercise.id === exerciseId)?.standard_name_zh ??
    exerciseId;

  const removeRecord = useMutation({
    mutationFn: (recordId: number) => deleteRecord(recordId),
    onSuccess: async () => {
      toast.success("训练记录已删除");
      await invalidateRecordDerivedQueries(queryClient);
    },
    onError: (error) =>
      toast.error(error instanceof Error ? error.message : "训练记录删除失败"),
  });

  const removeMetric = useMutation({
    mutationFn: (metricId: number) => deleteBodyMetric(metricId),
    onSuccess: async () => {
      toast.success("身体指标已删除");
      await invalidateRecordDerivedQueries(queryClient);
    },
    onError: (error) =>
      toast.error(error instanceof Error ? error.message : "身体指标删除失败"),
  });

  const editingRecord =
    typeof recordForm === "number"
      ? (records.data?.records.find((record) => record.id === recordForm) ??
        null)
      : null;
  const editingMetric =
    typeof metricForm === "number"
      ? (metrics.data?.metrics.find((metric) => metric.id === metricForm) ??
        null)
      : null;

  return (
    <div className="mx-auto w-full max-w-3xl px-6 pb-10">
      <header className="pt-10 pb-6">
        <h2 className="font-display text-3xl font-light tracking-tight">
          训练记录
        </h2>
        <p className="mt-1 text-sm text-muted-foreground">
          表单直接写入训练与身体数据；改动后统计立即按有效记录重算。
        </p>
      </header>

      <section className="flex flex-col gap-4">
        <div className="flex items-center justify-between">
          <h3 className="text-lg font-light tracking-tight">训练</h3>
          <Button
            size="sm"
            onClick={() => setRecordForm("new")}
            disabled={exercises.isPending || exercises.isError}
          >
            <Plus aria-hidden />
            新增训练记录
          </Button>
        </div>

        {exercises.isError && (
          <p className="text-sm text-destructive">
            动作目录加载失败：{exercises.error.message}，无法录入训练数据。
          </p>
        )}

        {recordForm !== null && (
          <RecordFormCard
            key={recordForm}
            record={editingRecord}
            exercises={catalogue}
            onDone={() => setRecordForm(null)}
          />
        )}

        {records.isPending && (
          <p className="text-sm text-muted-foreground">正在加载训练记录…</p>
        )}
        {records.isError && (
          <p className="text-sm text-destructive">
            加载训练记录失败：{records.error.message}，请刷新重试。
          </p>
        )}
        {records.data && records.data.records.length === 0 && (
          <p className="text-sm text-muted-foreground">
            暂无训练记录；用上面的表单新增一次训练。
          </p>
        )}

        {[...(records.data?.records ?? [])]
          .sort((a, b) => b.performed_on.localeCompare(a.performed_on))
          .map((record) => (
            <RecordCard
              key={record.id}
              record={record}
              nameOf={nameOf}
              onEdit={() => setRecordForm(record.id)}
              onDelete={async () => {
                await removeRecord.mutateAsync(record.id);
              }}
            />
          ))}
      </section>

      <section className="mt-10 flex flex-col gap-4">
        <div className="flex items-center justify-between">
          <h3 className="text-lg font-light tracking-tight">身体指标</h3>
          <Button size="sm" onClick={() => setMetricForm("new")}>
            <Plus aria-hidden />
            新增身体指标
          </Button>
        </div>

        {metricForm !== null && (
          <BodyMetricFormCard
            key={metricForm}
            metric={editingMetric}
            onDone={() => setMetricForm(null)}
          />
        )}

        {metrics.isPending && (
          <p className="text-sm text-muted-foreground">正在加载身体指标…</p>
        )}
        {metrics.isError && (
          <p className="text-sm text-destructive">
            加载身体指标失败：{metrics.error.message}，请刷新重试。
          </p>
        )}
        {metrics.data && metrics.data.metrics.length === 0 && (
          <p className="text-sm text-muted-foreground">
            暂无身体指标；记录体重与体脂后才能看到趋势。
          </p>
        )}

        <div className="flex flex-col gap-2">
          {[...(metrics.data?.metrics ?? [])]
            .sort((a, b) => b.measured_on.localeCompare(a.measured_on))
            .map((metric) => (
              <MetricRow
                key={metric.id}
                metric={metric}
                onEdit={() => setMetricForm(metric.id)}
                onDelete={async () => {
                  await removeMetric.mutateAsync(metric.id);
                }}
              />
            ))}
        </div>
      </section>
    </div>
  );
}

/** 一条身体指标：体脂未记录显示「未记录」，不显示 0 */
function MetricRow({
  metric,
  onEdit,
  onDelete,
}: {
  metric: BodyMetricWire;
  onEdit: () => void;
  onDelete: () => Promise<void>;
}) {
  const [confirming, setConfirming] = useState(false);
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border/60 px-3 py-2 text-sm">
      <span className="tabular-nums">{metric.measured_on}</span>
      <span className="tabular-nums">{metric.weight_kg} kg</span>
      <span className="text-muted-foreground">
        体脂：
        {metric.body_fat_pct === null ? "未记录" : `${metric.body_fat_pct}%`}
      </span>
      <span className="flex gap-2">
        <Button variant="outline" size="sm" onClick={onEdit}>
          编辑
        </Button>
        <Button
          variant={confirming ? "destructive" : "outline"}
          size="sm"
          onBlur={() => setConfirming(false)}
          onClick={() => {
            if (!confirming) {
              setConfirming(true);
              return;
            }
            void onDelete();
          }}
        >
          {confirming ? "确认删除？" : "删除"}
        </Button>
      </span>
    </div>
  );
}
