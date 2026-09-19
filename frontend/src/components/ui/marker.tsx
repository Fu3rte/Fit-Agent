import type * as React from "react";
import { cn } from "@/lib/utils";

function Marker({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="marker"
      className={cn(
        "group/marker relative flex min-h-4 w-full items-center gap-2 text-left text-sm text-muted-foreground [&_svg:not([class*='size-'])]:size-4",
        className,
      )}
      {...props}
    />
  );
}

function MarkerIcon({ className, ...props }: React.ComponentProps<"span">) {
  return (
    <span
      data-slot="marker-icon"
      aria-hidden="true"
      className={cn(
        "size-4 shrink-0 [&_svg:not([class*='size-'])]:size-4",
        className,
      )}
      {...props}
    />
  );
}

function MarkerContent({ className, ...props }: React.ComponentProps<"span">) {
  return (
    <span
      data-slot="marker-content"
      className={cn("min-w-0 wrap-break-word", className)}
      {...props}
    />
  );
}

export { Marker, MarkerIcon, MarkerContent };
