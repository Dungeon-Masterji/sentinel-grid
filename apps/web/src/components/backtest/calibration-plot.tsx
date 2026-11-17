"use client";

import { useState } from "react";
import { CartesianGrid, ComposedChart, Line, ReferenceLine, ResponsiveContainer, Scatter, Tooltip, XAxis, YAxis } from "recharts";
import { Button } from "@/components/ui/button";
import { SCENARIO, type Calibration } from "@/lib/backtest";

const INK = "var(--foreground)";
const MUTED = "var(--muted-foreground)";

/** Reliability diagram: predicted fused score (bin mean) vs observed event frequency. */
export function CalibrationPlot({ overall, perScenario }: { overall: Calibration; perScenario: Record<string, Calibration> }) {
  const [which, setWhich] = useState<string>("overall");
  const [zoom, setZoom] = useState(false);
  const cal = which === "overall" ? overall : perScenario[which];
  const color = which === "overall" ? INK : SCENARIO[which]?.color ?? INK;
  const pts = (cal?.bins ?? []).filter((b) => b.n > 0 && b.mean_predicted != null)
    .map((b) => ({ x: b.mean_predicted as number, y: b.observed_frequency as number, n: b.n, lo: b.lo, hi: b.hi }));
  const dataMax = Math.max(0.001, ...pts.map((p) => p.y));
  const yMax = zoom ? Math.min(1, Math.ceil(dataMax * 125) / 100) : Math.max(1, dataMax);
  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-1" role="group" aria-label="Calibration series">
        {["overall", ...Object.keys(perScenario)].map((k) => (
          <Button key={k} size="xs" variant={which === k ? "default" : "outline"} aria-pressed={which === k} onClick={() => setWhich(k)}>
            {k === "overall" ? "All scenarios" : SCENARIO[k]?.label ?? k}
          </Button>
        ))}
        <Button size="xs" variant={zoom ? "default" : "ghost"} aria-pressed={zoom} className="ml-auto" onClick={() => setZoom((z) => !z)}>
          {zoom ? "Full 0–1 scale" : "Zoom to data"}
        </Button>
      </div>
      <div className="mb-1 text-xs text-muted-foreground tabular-nums">
        Brier <span className="font-medium text-foreground">{cal?.brier?.toFixed(3) ?? "—"}</span> · always-predict-base-rate{" "}
        {cal?.brier_climatology?.toFixed(3) ?? "—"} · skill {cal?.brier_skill?.toFixed(2) ?? "—"} · {cal?.n?.toLocaleString()} eligible
        scenario-hex-hours, {cal?.positives?.toLocaleString()} inside an event
      </div>
      <div className="h-72">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart margin={{ top: 8, right: 16, bottom: 24, left: 4 }}>
            <CartesianGrid stroke="var(--border)" strokeOpacity={0.6} />
            <XAxis type="number" dataKey="x" domain={[0, 1]} ticks={[0, 0.2, 0.4, 0.6, 0.8, 1]} tick={{ fontSize: 11, fill: MUTED }}
              stroke="var(--border)" label={{ value: "predicted (mean fused score in bin)", position: "insideBottom", offset: -14, fontSize: 11, fill: MUTED }} />
            <YAxis type="number" dataKey="y" domain={[0, yMax]} tick={{ fontSize: 11, fill: MUTED }} stroke="var(--border)" width={40}
              label={{ value: "observed event frequency", angle: -90, position: "insideLeft", offset: 10, fontSize: 11, fill: MUTED }} />
            <ReferenceLine segment={[{ x: 0, y: 0 }, { x: yMax, y: yMax }]} stroke={MUTED} strokeDasharray="4 4"
              label={{ value: "perfect calibration", position: "insideTopLeft", fontSize: 10, fill: MUTED }} />
            <Tooltip
              cursor={{ strokeDasharray: "3 3" }}
              content={({ active, payload }) => {
                const p = active && (payload?.[0]?.payload as (typeof pts)[number] | undefined);
                if (!p) return null;
                return (
                  <div className="rounded-md border bg-popover px-2 py-1.5 text-[11px] shadow-sm tabular-nums">
                    <div className="font-medium">score bin {p.lo.toFixed(1)}–{p.hi.toFixed(1)}</div>
                    <div>predicted {p.x.toFixed(3)} · observed {p.y.toFixed(4)}</div>
                    <div className="text-muted-foreground">{p.n.toLocaleString()} hex-hours</div>
                  </div>
                );
              }}
            />
            <Line data={pts} type="linear" dataKey="y" stroke={color} strokeWidth={2} dot={false} isAnimationActive={false} />
            <Scatter data={pts} fill={color} stroke="var(--popover)" strokeWidth={2} shape="circle" isAnimationActive={false} />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      <p className="text-[11px] text-muted-foreground">
        The fused score is P(scenario firing) under the fusion model, not a calibrated probability that a
        ground-truth event is under way; points far below the diagonal say exactly that.
      </p>
    </div>
  );
}
