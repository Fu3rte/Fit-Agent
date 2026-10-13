import { renderToStaticMarkup } from "react-dom/server";
import WorkoutFields from "@/features/workout/WorkoutFields";
import type { WorkoutContentWire } from "@/lib/contract";

export function renderWorkoutFields(content: WorkoutContentWire): string {
  return renderToStaticMarkup(<WorkoutFields content={content} />);
}
