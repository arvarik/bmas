/**
 * Gate rule shaping shared by the baseline form and its tests.
 *
 * A frozen method fixes the operator: non-inferiority declares its
 * margin through `max_drop`, superiority declares strict improvement
 * through `gte`. The serializer keeps only the fields the daemon
 * accepts for each method.
 */
import type { RegressionAnalysisMethod, RegressionOperator, RegressionRule } from "@/lib/benchmarks";

export const METHOD_LABELS: Record<RegressionAnalysisMethod, string> = {
  point_estimate: "Point estimate",
  lower_confidence_bound: "Lower 95% bound",
  upper_confidence_bound: "Upper 95% bound",
  holm_sign_test: "Holm-adjusted sign test",
  frozen_non_inferiority: "Frozen non-inferiority",
  frozen_superiority: "Frozen superiority",
};

export interface RuleShape {
  operator: RegressionOperator | null;
  limitLabel: string;
  frozen: boolean;
}

/** The rule shape one analysis method needs: operator, limit label, and frozen fields. */
export function ruleShapeForMethod(method: RegressionAnalysisMethod): RuleShape {
  if (method === "frozen_non_inferiority") return { operator: "max_drop", limitLabel: "Margin (0 to 1)", frozen: true };
  if (method === "frozen_superiority") return { operator: "gte", limitLabel: "Limit (unused, 0)", frozen: true };
  return { operator: null, limitLabel: "Limit", frozen: false };
}

/** Keep only the fields the daemon accepts for each rule's method. */
export function serializeRules(rules: RegressionRule[]): RegressionRule[] {
  return rules.map((rule) => {
    const method = rule.analysis_method ?? "point_estimate";
    const shape = ruleShapeForMethod(method);
    const serialized: RegressionRule = {
      id: rule.id,
      label: rule.label,
      metric: rule.metric,
      operator: shape.operator ?? rule.operator,
      value: method === "frozen_superiority" ? 0 : rule.value,
      analysis_method: method,
    };
    if (shape.frozen) {
      serialized.direction = rule.direction ?? "improvement";
      if (rule.resample_count) serialized.resample_count = rule.resample_count;
      if (rule.minimum_usable_cases) serialized.minimum_usable_cases = rule.minimum_usable_cases;
    } else if (rule.direction) {
      serialized.direction = rule.direction;
    }
    return serialized;
  });
}
