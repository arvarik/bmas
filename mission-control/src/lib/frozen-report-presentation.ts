/**
 * Presentation helpers for the frozen report and frozen gate decisions.
 *
 * Every number the screen shows comes from the daemon's frozen
 * snapshot; the helpers only format, label, and lay out the decision
 * display. The decision bar follows the forest-plot convention for a
 * non-inferiority comparison: the interval, its point estimate, the
 * zero line, and the predeclared margin share one axis, so the reader
 * sees whether the interval clears the margin.
 */
import {
  isFrozenReport,
  type FrozenComparison,
  type FrozenGateDecision,
  type FrozenInterval,
  type FrozenRuleBlock,
  type FrozenRunReport,
  type RunReportResponse,
} from "@/lib/benchmarks";

export type DecisionTone = "passed" | "failed" | "indeterminate";

export interface DecisionSummary {
  tone: DecisionTone;
  label: string;
  rule: string;
  detail: string;
}

/** Format a paired difference in success proportion as percentage points. */
export function formatDifference(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "Unavailable";
  const points = value * 100;
  const sign = points > 0 ? "+" : "";
  return `${sign}${points.toFixed(1)} pp`;
}

export function formatProbability(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "Unavailable";
  return value < 0.001 ? value.toFixed(4) : value.toFixed(3);
}

export function formatIntervalText(interval: FrozenInterval | undefined): string {
  if (!interval) return "Unavailable";
  if (interval.status === "estimated" || interval.status === "degenerate") {
    return `${formatDifference(interval.low)} to ${formatDifference(interval.high)}`;
  }
  if (interval.status === "insufficient") return "Insufficient cases";
  if (interval.status === "no_data") return "No paired data";
  return interval.status.replaceAll("_", " ");
}

const RULE_TEXT: Record<string, string> = {
  lower_bound_above_negative_margin: "the lower bound stays above the negative margin",
  upper_bound_below_margin: "the upper bound stays below the margin",
  holm_adjusted_significance_and_interval_excludes_zero:
    "the Holm-adjusted test is significant and the interval excludes zero",
};

/** One sentence that explains a frozen gate decision. */
export function decisionSummary(
  gate: FrozenGateDecision | undefined,
  hypothesis: "non_inferiority" | "superiority" | undefined,
): DecisionSummary {
  if (!gate) {
    return { tone: "indeterminate", label: "Indeterminate", rule: "", detail: "No frozen decision exists." };
  }
  if (gate.status === "indeterminate") {
    const reasons = gate.reasons.map((reason) => reason.replaceAll("_", " ")).join(", ");
    return {
      tone: "indeterminate",
      label: "Indeterminate",
      rule: "",
      detail: reasons ? `Blocked by ${reasons}.` : "The preconditions did not hold.",
    };
  }
  const rule = gate.rule ? RULE_TEXT[gate.rule] ?? gate.rule.replaceAll("_", " ") : "";
  const parts: string[] = [];
  if (hypothesis === "non_inferiority") {
    parts.push(`Bound ${formatDifference(gate.bound)}`);
    parts.push(`margin ${formatDifference(gate.margin === null || gate.margin === undefined ? null : -Math.abs(gate.margin))}`);
  } else if (gate.p_value_adjusted !== undefined) {
    parts.push(`Holm-adjusted p ${formatProbability(gate.p_value_adjusted)}`);
  }
  return {
    tone: gate.status,
    label: gate.status === "passed" ? "Passed" : "Failed",
    rule,
    detail: parts.join(", ") + (parts.length ? "." : ""),
  };
}

export interface ForestGeometry {
  width: number;
  domain: number;
  zeroX: number;
  marginX: number | null;
  lowX: number | null;
  highX: number | null;
  estimateX: number | null;
  ticks: Array<{ x: number; label: string }>;
}

function scale(value: number, domain: number, width: number): number {
  const clamped = Math.max(-domain, Math.min(domain, value));
  return ((clamped + domain) / (2 * domain)) * width;
}

/**
 * Lay out one comparison on a symmetric axis in percentage points.
 *
 * The domain grows to fit the interval and the margin so nothing
 * clips, and it never shrinks below five points so a tight interval
 * still reads as a bar.
 */
export function forestGeometry(
  comparison: Pick<FrozenComparison, "estimate" | "interval" | "non_inferiority_margin" | "direction" | "hypothesis">,
  width = 240,
): ForestGeometry {
  const { interval } = comparison;
  const usable = interval && (interval.status === "estimated" || interval.status === "degenerate");
  const low = usable ? interval.low : null;
  const high = usable ? interval.high : null;
  const margin = comparison.hypothesis === "non_inferiority" && comparison.non_inferiority_margin !== null
    ? Math.abs(comparison.non_inferiority_margin) : null;
  const magnitudes = [low, high, comparison.estimate, margin]
    .filter((value): value is number => value !== null && value !== undefined)
    .map(Math.abs);
  const domain = Math.max(0.05, ...magnitudes) * 1.15;
  const signedMargin = margin === null ? null : comparison.direction === "lower_is_better" ? margin : -margin;
  const ticks = [-domain, 0, domain].map((value) => ({
    x: scale(value, domain, width),
    label: `${value > 0 ? "+" : ""}${(value * 100).toFixed(0)}`,
  }));
  return {
    width,
    domain,
    zeroX: scale(0, domain, width),
    marginX: signedMargin === null ? null : scale(signedMargin, domain, width),
    lowX: low === null ? null : scale(low, domain, width),
    highX: high === null ? null : scale(high, domain, width),
    estimateX: comparison.estimate === null || comparison.estimate === undefined
      ? null : scale(comparison.estimate, domain, width),
    ticks,
  };
}

/** Describe the engine that produced a served report. */
export function reportEngineLabel(report: RunReportResponse): { label: string; tone: "frozen" | "legacy" } {
  if (isFrozenReport(report)) return { label: "Frozen snapshot", tone: "frozen" };
  return { label: "Legacy report engine", tone: "legacy" };
}

export interface MetricResolutionSummary {
  resolved: number;
  unresolved: number;
  status: "resolved" | "partial" | "none";
  statement: string;
}

/** Summarize how many displayed metrics resolve to published definitions. */
export function metricResolutionSummary(report: FrozenRunReport): MetricResolutionSummary {
  const resolved = report.metrics.length;
  const unresolved = report.unresolved_metrics.length;
  if (unresolved === 0 && resolved > 0) {
    return { resolved, unresolved, status: "resolved", statement: `Every displayed metric resolves to a published definition (${resolved}).` };
  }
  if (resolved === 0) {
    return { resolved, unresolved, status: "none", statement: "No displayed metric resolves to a published definition yet." };
  }
  return { resolved, unresolved, status: "partial", statement: `${resolved} resolved, ${unresolved} unresolved.` };
}

/** Turn the frozen block of one gate rule into a comparison-shaped view. */
export function ruleComparison(rule: {
  frozen?: FrozenRuleBlock;
  direction?: "improvement" | "reduction" | null;
  analysis_method?: string;
  value: number;
}): Pick<FrozenComparison, "estimate" | "interval" | "non_inferiority_margin" | "direction" | "hypothesis"> | null {
  const block = rule.frozen;
  if (!block || !block.interval) return null;
  return {
    estimate: block.estimate ?? null,
    interval: block.interval,
    non_inferiority_margin: rule.analysis_method === "frozen_non_inferiority" ? rule.value : null,
    direction: rule.direction === "reduction" ? "lower_is_better" : "higher_is_better",
    hypothesis: rule.analysis_method === "frozen_superiority" ? "superiority" : "non_inferiority",
  };
}

export function replayVerificationLabel(report: FrozenRunReport): { label: string; tone: DecisionTone } {
  if (report.replay_verified) return { label: "Replay verified", tone: "passed" };
  return { label: "Replay digest differs", tone: "failed" };
}
