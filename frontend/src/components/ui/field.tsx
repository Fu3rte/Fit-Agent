import type * as React from "react";
import { cn } from "@/lib/utils";

const FIELD_LABEL = "flex w-fit gap-2 text-sm font-medium leading-snug";

function Field({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      role="group"
      data-slot="field"
      className={cn("flex w-full flex-col gap-3", className)}
      {...props}
    />
  );
}

function FieldLabel({ className, ...props }: React.ComponentProps<"label">) {
  return (
    <label
      data-slot="field-label"
      className={cn(FIELD_LABEL, className)}
      {...props}
    />
  );
}

function FieldTitle({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="field-title"
      className={cn(FIELD_LABEL, className)}
      {...props}
    />
  );
}

export { Field, FieldLabel, FieldTitle };
