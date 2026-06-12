"use client";

/**
 * ConvergenceStrip — Bottom sparkline strip for Mission cockpit.
 *
 * Shows per-round sparklines:
 *   - Open critiques count (falling)
 *   - Convergence signal (rising)
 *   - Budget spend (climbing toward ceiling)
 *
 * Data from consensus + budget SSE events, accumulated per round.
 *
 * @module Phase 5 (doc 13 §5)
 */

import { useMemo } from "react";
import {
  ResponsiveContainer,
  AreaChart,
  Area,
  Tooltip,
  YAxis,
} from "recharts";
import type { BudgetState, CoordinatorNarration } from "@/hooks/useTaskStream";
import type { BoardEntryData } from "@/hooks/useTaskStream";
import { STATUS_COLORS, HEAT_RAMP } from "@/lib/design-tokens";

interface ConvergenceStripProps {
  entries: BoardEntryData[];
  budgetState: BudgetState | null;
  narrations: CoordinatorNarration[];
}

interface RoundDatum {
  round: number;
  openCritiques: number;
  convergence: number;
  budgetPct: number;
}

export function ConvergenceStrip({
  entries,
  budgetState,
  narrations,
}: ConvergenceStripProps) {
  // Compute per-round metrics
  const data = useMemo<RoundDatum[]>(() => {
    if (!entries.length && !narrations.length) return [];

    // Group entries by round
    const maxRound = Math.max(
      ...entries.map((e) => e.round ?? 0),
      ...narrations.map((n) => n.round),
      1,
    );

    const result: RoundDatum[] = [];
    for (let r = 1; r <= maxRound; r++) {
      const roundEntries = entries.filter((e) => (e.round ?? 0) <= r);
      const openCritiques = roundEntries.filter(
        (e) => e.type === "critique" && e.status === "open",
      ).length;
      const totalOpen = roundEntries.filter(
        (e) => e.status === "open",
      ).length;
      const solutions = roundEntries.filter(
        (e) => e.type === "solution" && e.status === "open",
      ).length;

      // Convergence: ratio of solutions to total open + inverse of critiques
      const convergence =
        totalOpen > 0
          ? Math.min(1, (solutions * 2 + (totalOpen - openCritiques)) / (totalOpen + 1))
          : 0;

      result.push({
        round: r,
        openCritiques,
        convergence: Math.round(convergence * 100),
        budgetPct: budgetState ? budgetState.percentage : 0,
      });
    }
    return result;
  }, [entries, narrations, budgetState]);

  if (data.length === 0) {
    return (
      <div className="convergence-strip convergence-strip--empty">
        <span style={{ color: "hsl(215, 15%, 65%)", fontSize: 11 }}>
          Convergence data appears after round 1
        </span>
      </div>
    );
  }

  return (
    <div className="convergence-strip">
      {/* Open Critiques sparkline */}
      <div className="convergence-strip__chart">
        <span className="convergence-strip__label">Open Critiques</span>
        <ResponsiveContainer width="100%" height={36}>
          <AreaChart data={data} margin={{ top: 2, right: 2, bottom: 0, left: 0 }}>
            <YAxis hide domain={[0, "auto"]} />
            <Tooltip
              contentStyle={{
                background: "hsl(222, 44%, 9%)",
                border: "1px solid hsl(222, 30%, 18%)",
                borderRadius: 6,
                fontSize: 10,
              }}
              labelFormatter={(v) => `Round ${v}`}
            />
            <Area
              type="monotone"
              dataKey="openCritiques"
              stroke={HEAT_RAMP[2]}
              fill={`${HEAT_RAMP[2]}22`}
              strokeWidth={1.5}
              dot={false}
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>

      {/* Convergence signal sparkline */}
      <div className="convergence-strip__chart">
        <span className="convergence-strip__label">Convergence %</span>
        <ResponsiveContainer width="100%" height={36}>
          <AreaChart data={data} margin={{ top: 2, right: 2, bottom: 0, left: 0 }}>
            <YAxis hide domain={[0, 100]} />
            <Tooltip
              contentStyle={{
                background: "hsl(222, 44%, 9%)",
                border: "1px solid hsl(222, 30%, 18%)",
                borderRadius: 6,
                fontSize: 10,
              }}
              labelFormatter={(v) => `Round ${v}`}
            />
            <Area
              type="monotone"
              dataKey="convergence"
              stroke={STATUS_COLORS.success}
              fill={`${STATUS_COLORS.success}22`}
              strokeWidth={1.5}
              dot={false}
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>

      {/* Budget spend sparkline */}
      <div className="convergence-strip__chart">
        <span className="convergence-strip__label">Budget %</span>
        <ResponsiveContainer width="100%" height={36}>
          <AreaChart data={data} margin={{ top: 2, right: 2, bottom: 0, left: 0 }}>
            <YAxis hide domain={[0, 100]} />
            <Tooltip
              contentStyle={{
                background: "hsl(222, 44%, 9%)",
                border: "1px solid hsl(222, 30%, 18%)",
                borderRadius: 6,
                fontSize: 10,
              }}
              labelFormatter={(v) => `Round ${v}`}
            />
            <Area
              type="monotone"
              dataKey="budgetPct"
              stroke={HEAT_RAMP[1]}
              fill={`${HEAT_RAMP[1]}22`}
              strokeWidth={1.5}
              dot={false}
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
