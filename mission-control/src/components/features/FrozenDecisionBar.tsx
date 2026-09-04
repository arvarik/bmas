import { forestGeometry } from "@/lib/frozen-report-presentation";
import type { FrozenComparison } from "@/lib/benchmarks";

type Comparison = Pick<FrozenComparison, "estimate" | "interval" | "non_inferiority_margin" | "direction" | "hypothesis">;

/**
 * One forest-plot row: the interval bar, its point estimate, the zero
 * line, and the predeclared margin on a shared axis in percentage
 * points. The bar carries a text description for assistive technology.
 */
export function FrozenDecisionBar({ comparison, label, tone }: { comparison: Comparison; label: string; tone: "passed" | "failed" | "indeterminate" }) {
  const geometry = forestGeometry(comparison);
  const height = 34;
  const mid = 15;
  const usable = geometry.lowX !== null && geometry.highX !== null;
  const description = usable
    ? `${label}: interval from ${((comparison.interval.low ?? 0) * 100).toFixed(1)} to ${((comparison.interval.high ?? 0) * 100).toFixed(1)} percentage points` +
      (geometry.marginX !== null ? `, margin at ${((comparison.non_inferiority_margin ?? 0) * -100).toFixed(1)} points` : "")
    : `${label}: no interval is available`;
  return (
    <svg
      className={`frozen-decision-bar frozen-decision-bar--${tone}`}
      width={geometry.width}
      height={height}
      viewBox={`0 0 ${geometry.width} ${height}`}
      role="img"
      aria-label={description}
    >
      <line className="frozen-decision-bar__axis" x1={0} x2={geometry.width} y1={mid} y2={mid} />
      <line className="frozen-decision-bar__zero" x1={geometry.zeroX} x2={geometry.zeroX} y1={2} y2={height - 10} />
      {geometry.marginX !== null ? (
        <line className="frozen-decision-bar__margin" x1={geometry.marginX} x2={geometry.marginX} y1={2} y2={height - 10} strokeDasharray="3 2" />
      ) : null}
      {usable ? (
        <>
          <rect
            className="frozen-decision-bar__interval"
            x={Math.min(geometry.lowX as number, geometry.highX as number)}
            width={Math.max(2, Math.abs((geometry.highX as number) - (geometry.lowX as number)))}
            y={mid - 5}
            height={10}
            rx={2}
          />
          {geometry.estimateX !== null ? (
            <circle className="frozen-decision-bar__estimate" cx={geometry.estimateX} cy={mid} r={3.5} />
          ) : null}
        </>
      ) : (
        <text className="frozen-decision-bar__note" x={geometry.zeroX + 6} y={mid + 4}>no interval</text>
      )}
      {geometry.ticks.map((tick) => (
        <text key={tick.label} className="frozen-decision-bar__tick" x={tick.x} y={height - 1} textAnchor={tick.x === 0 ? "start" : tick.x === geometry.width ? "end" : "middle"}>{tick.label}</text>
      ))}
    </svg>
  );
}
