import type { ReactNode } from "react";
import { Badge } from "@/components/ui/badge";
import type { FieldDiff } from "@/lib/contract";

/* ------------------------------ 可编辑字段 -------------------------------- */

export const selectClass =
  "h-7 rounded-md border border-input bg-transparent px-2 text-xs disabled:opacity-50";

export const toNumber = (v: string): number | undefined => {
  if (v.trim() === "") return undefined;
  const n = Number(v);
  return Number.isFinite(n) ? n : undefined;
};

/** 选项里补上草稿自带的非表内取值，避免编辑后 select 丢失原值 */
export const withCurrent = (options: string[], current?: string): string[] =>
  current !== undefined && !options.includes(current)
    ? [...options, current]
    : options;

/* ------------------------------- Diff 列表 -------------------------------- */

/** 单行字段 Diff（旧值→新值，无旧值标「新增」） */
export function FieldRow({ row }: { row: FieldDiff }) {
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs">
      <span className="min-w-32 text-muted-foreground">{row.field}</span>
      {row.old_value === undefined ? (
        <Badge variant="secondary" className="text-[10px]">
          新增
        </Badge>
      ) : (
        <span className="rounded bg-muted px-1.5 py-0.5 text-muted-foreground line-through decoration-destructive/60">
          {row.old_value}
        </span>
      )}
      <span aria-hidden className="text-muted-foreground">
        →
      </span>
      <span className="rounded bg-bubble-out px-1.5 py-0.5 font-medium text-bubble-out-foreground">
        {row.new_value}
      </span>
    </div>
  );
}

export function FieldDiffList({ rows }: { rows: FieldDiff[] }) {
  if (rows.length === 0) return null;
  return (
    <div className="space-y-1.5">
      {rows.map((row, i) => (
        <FieldRow key={i} row={row} />
      ))}
    </div>
  );
}

export function FieldLine({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs">
      <span className="min-w-24 text-muted-foreground">{label}</span>
      {children}
    </div>
  );
}

export function ListField({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <div className="text-xs">
      <div className="mb-1.5 text-muted-foreground">{label}</div>
      <div className="space-y-1.5">{children}</div>
    </div>
  );
}
