# Benchmark Statistics

[Return to the documentation index](README.md).

The analysis uses paired observations. A pair contains the left-arm and right-arm scores for the same dataset item and repetition.

## Analysis contract

The current report uses analysis schema version 2.

| Output | Method |
|:---|:---|
| Mean uncertainty | Deterministic 95 percent bias-corrected and accelerated bootstrap interval |
| Paired evidence | Exact two-sided sign test without ties |
| Multiple comparisons | Holm-Bonferroni family-wise error correction |
| Practical importance | Test-revision minimum practical score difference |
| Effect summaries | Mean paired difference, standardized paired effect, and probability of superiority |
| Sample guidance | Normal approximation for 80 percent power |

The implementation uses 999 bootstrap resamples. A stable seed comes from the analysis version, metric identity, and input values.

The report checksum stays stable when the same normalized input produces the same output.

SciPy documents the BCa method in its [bootstrap reference](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.bootstrap.html). Statsmodels documents the Holm method in its [multiple-test reference](https://www.statsmodels.org/stable/generated/statsmodels.stats.multitest.multipletests.html).

The daemon implements these methods directly. It does not import SciPy or Statsmodels at runtime.

## Read an interval

The interval estimates uncertainty around a mean. It does not guarantee that a future run lands inside the range.

The report returns no bounds for one observation. It also returns no bounds when every bootstrap mean is identical.

This behavior prevents a small or degenerate sample from appearing certain.

## Read a paired comparison

The delta equals the right-arm score minus the left-arm score.

- A positive delta favors the right arm.
- A negative delta favors the left arm.
- A zero delta counts as a tie.

The sign test uses wins and losses. It removes ties from the test count.

The adjusted probability controls the family-wise error rate across all paired scorer comparisons in one report.

## Read the diagnosis

The report combines the corrected probability, interval, and practical difference.

| Diagnosis | Meaning |
|:---|:---|
| Insufficient sample | The comparison has fewer than two pairs. |
| Within practical range | The complete interval stays inside the practical-equivalence range. |
| Inconclusive | The adjusted probability exceeds 0.05 or an adjusted probability is unavailable. |
| Meaningful improvement | The corrected result passes and the complete interval exceeds the positive practical threshold. |
| Meaningful regression | The corrected result passes and the complete interval falls below the negative practical threshold. |
| Statistically detectable but not practical | The corrected result passes, but the interval does not clear the practical threshold. |

Do not select a winner from the point estimate alone. Review the interval, adjusted probability, failure rate, and practical threshold together.

## Sample guidance

The recommended pair count uses the observed paired standard deviation and the configured practical difference. It targets 80 percent power with a normal approximation.

Treat this value as planning guidance. It is not a stopping rule and it does not correct adaptive repeated testing.

The report returns no recommendation when it cannot estimate variance or the practical difference is zero.

## Diagnostic slices

The report creates bounded subject, split, and tag slices. It uses the same interval method inside each slice.

Use slices to locate a change. Do not treat many exploratory slice results as independent confirmed findings.

The report keeps at most 100 slice identities and 500 largest per-item differences. These limits keep report time and payload size bounded.

## Human calibration

Human reviews produce these diagnostics when paired automatic scores exist:

- pass-decision agreement
- Cohen kappa
- mean absolute score error
- Brier score against the human pass decision

Kappa can become unavailable when the expected agreement equals one. A high raw agreement does not imply useful calibration when one class dominates.

## Regression gates

Each rule selects one analysis method:

| Method | Selected value |
|:---|:---|
| `point_estimate` | The requested metric value. |
| `lower_confidence_bound` | The metric lower bound. |
| `upper_confidence_bound` | The metric upper bound. |
| `holm_sign_test` | The paired comparison's adjusted sign-test probability. |

Use a lower bound for a minimum-quality rule. Use an upper bound for a maximum mean-cost rule.

Failure rate does not publish a confidence interval in analysis version 2. Use its point estimate until a later analysis contract adds that interval.

Use `holm_sign_test` only with a paired comparison metric. A missing method value makes the gate indeterminate.
