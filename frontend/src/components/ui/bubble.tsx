import type * as React from "react";
import { cn } from "@/lib/utils";

/* default=用户 bubble-out，secondary=助手 bubble-in */
function Bubble({
  variant = "default",
  align = "start",
  className,
  ...props
}: React.ComponentProps<"div"> & {
  variant?: "default" | "secondary";
  align?: "start" | "end";
}) {
  return (
    <div
      data-slot="bubble"
      data-variant={variant}
      data-align={align}
      className={cn(
        "group/bubble relative flex w-fit max-w-[85%] min-w-0 flex-col gap-1 group-data-[align=end]/message:self-end data-[align=end]:self-end",
        variant === "default"
          ? "*:data-[slot=bubble-content]:bg-bubble-out *:data-[slot=bubble-content]:text-bubble-out-foreground *:data-[slot=bubble-content]:inset-ring-1 *:data-[slot=bubble-content]:inset-ring-bubble-out-border"
          : "*:data-[slot=bubble-content]:bg-bubble-in *:data-[slot=bubble-content]:text-bubble-in-foreground *:data-[slot=bubble-content]:inset-ring-1 *:data-[slot=bubble-content]:inset-ring-border",
        className,
      )}
      {...props}
    />
  );
}

function BubbleContent({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="bubble-content"
      className={cn(
        "w-fit max-w-full min-w-0 overflow-hidden rounded-xl border border-transparent px-4 py-3 text-sm leading-relaxed wrap-break-word group-data-[align=end]/bubble:self-end",
        className,
      )}
      {...props}
    />
  );
}

export { Bubble, BubbleContent };
