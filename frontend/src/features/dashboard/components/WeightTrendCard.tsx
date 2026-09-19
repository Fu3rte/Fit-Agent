import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip as ChartTooltip,
  XAxis,
  YAxis,
} from "recharts";
import { TrendingUp } from "lucide-react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import type { TrendsWire } from "@/lib/contract";

/** 体重变化量：后端差值是浮点结果，展示口径四舍五入到一位小数 */
function formatDelta(value: number): string {
  const rounded = Math.round(value * 10) / 10;
  return `${rounded > 0 ? "+" : ""}${rounded.toFixed(1)}`;
}

function KpiValue({
  label,
  value,
  date,
}: {
  label: string;
  value: string | null;
  date: string | null;
}) {
  return (
    <div className="min-w-0">
      <span className="text-xs text-muted-foreground">{label}</span>
      <p className="font-mono text-2xl font-bold tabular-nums">
        {value ?? "—"}
        {value !== null && (
          <span className="ml-0.5 text-xs font-normal text-muted-foreground">
            kg
          </span>
        )}
      </p>
      <p className="truncate text-[10px] text-muted-foreground">
        {date ?? ""}
      </p>
    </div>
  );
}

/** 体重卡片：无点即空态，不补 0 */
export default function WeightTrendCard({ trends }: { trends: TrendsWire }) {
  const points = trends.weight;
  const change = trends.trend_summary.weight_change;
  const earliest = points.length === 0 ? null : points[0];

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <div>
            <CardTitle className="mb-2 flex items-center gap-2">
              <TrendingUp className="size-4" />
              体重记录
            </CardTitle>
            <CardDescription>
              最近 {trends.window_days} 天的体重变化
            </CardDescription>
          </div>
        </div>

        <div className="grid grid-cols-3 gap-3 border-b pt-3 pb-4">
          <KpiValue
            label="窗口首条"
            value={earliest === null ? null : String(earliest.value)}
            date={earliest?.measured_on ?? null}
          />
          <KpiValue
            label="最新体重"
            value={change.current === null ? null : String(change.current)}
            date={change.current_on}
          />
          <KpiValue
            label="上一条变化"
            value={change.change === null ? null : formatDelta(change.change)}
            date={
              change.previous === null
                ? null
                : `${change.previous_on} ${change.previous}kg`
            }
          />
        </div>
      </CardHeader>

      <CardContent className="flex flex-col gap-3">
        {points.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            暂无记录，暂不绘制折线。
          </p>
        ) : (
          <div className="h-64 w-full">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart
                data={points}
                margin={{ top: 8, right: 16, bottom: 0, left: 0 }}
              >
                <defs>
                  <linearGradient
                    id="weight-fill"
                    x1="0"
                    y1="0"
                    x2="0"
                    y2="1"
                  >
                    <stop
                      offset="5%"
                      stopColor="var(--primary)"
                      stopOpacity={0.25}
                    />
                    <stop
                      offset="95%"
                      stopColor="var(--primary)"
                      stopOpacity={0}
                    />
                  </linearGradient>
                </defs>
                <CartesianGrid
                  stroke="var(--border)"
                  strokeDasharray="3 3"
                  vertical={false}
                />
                <XAxis
                  dataKey="measured_on"
                  tick={{ fontSize: 11 }}
                  tickLine={false}
                  axisLine={false}
                  stroke="var(--muted-foreground)"
                  padding={{ left: 8, right: 8 }}
                  tickMargin={9}
                />
                <YAxis
                  domain={["auto", "auto"]}
                  tick={{ fontSize: 11 }}
                  tickLine={false}
                  axisLine={false}
                  stroke="var(--muted-foreground)"
                  unit="kg"
                  width={64}
                  tickMargin={8}
                />
                <ChartTooltip
                  contentStyle={{
                    background: "var(--card)",
                    border: "1px solid var(--border)",
                    borderRadius: "0.5rem",
                    fontSize: 12,
                    color: "var(--card-foreground)",
                  }}
                  formatter={(value) => [`${String(value)} kg`, "体重"]}
                />
                <Area
                  type="monotone"
                  dataKey="value"
                  stroke="var(--primary)"
                  strokeWidth={2}
                  fill="url(#weight-fill)"
                  dot={{ r: 3, fill: "var(--primary)" }}
                  isAnimationActive={false}
                />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        )}
        <p className="text-xs text-muted-foreground">
          时间 {trends.from} 至 {trends.to}；窗口内无记录的日期不出现点。
        </p>
      </CardContent>
    </Card>
  );
}
