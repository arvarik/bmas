"use client";

import type { VariantRuntimeState } from "@/hooks/useTaskStream";

export function UnsupportedVariantState({
  runtime,
  feature,
}: {
  runtime: VariantRuntimeState;
  feature?: string;
}) {
  const title = runtime.status === "loading"
    ? "Loading coordination interface"
    : feature
      ? "Interface feature unavailable"
      : "Coordination interface unavailable";
  const message = feature
    ? `The daemon does not advertise the ${feature} feature for this task.`
    : runtime.message;
  return (
    <div className="view-container">
      <div className="overview__error-card" role={runtime.status === "loading" ? "status" : "alert"}>
        <div className="overview__error-header">
          <h3>{title}</h3>
        </div>
        <div className="overview__error-body">{message}</div>
      </div>
    </div>
  );
}
