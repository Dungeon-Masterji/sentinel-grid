"use client";

import { PILLAR, signalLabel, type DriverDetail, type Pillar } from "@/lib/alerts";

type Segment = { key: string; pct: number; pillar: Pillar; color: string };

/**
 * Horizontal stacked bar of driver contributions (percent, summing to ~100).
 * Today the values are weight × anomaly-score shares from the fusion detector; Chunk 4
 * passes SHAP attributions in the same `drivers` shape: no component change needed.
 */
export function DriverBar({
  drivers,
  details,
  showLegend = true,
  height = "h-2.5",
}: {
  drivers: Record<string, number>;
  details?: Record<string, Pick<DriverDetail, "pillar">>;
  showLegend?: boolean;
  height?: string;
}) {
  const used: Record<Pillar, number> = { env: 0, occ: 0, clin: 0 };
  const segs: Segment[] = Object.entries(drivers)
    .filter(([, v]) => v > 0)
    .sort((a, b) => b[1] - a[1])
    .map(([key, pct]) => {
      const pillar = (details?.[key]?.pillar ?? "env") as Pillar;
      const shades = PILLAR[pillar].shades;
      const color = shades[used[pillar]++ % shades.length];
      return { key, pct, pillar, color };
    });
  const total = segs.reduce((s, x) => s + x.pct, 0) || 1;

  return (
    <div>
      <div
        className={`flex w-full overflow-hidden rounded-full bg-slate-100 dark:bg-slate-800 ${height}`}
        role="img"
        aria-label={segs.map((s) => `${signalLabel(s.key)} ${s.pct.toFixed(0)}%`).join(", ")}
      >
        {segs.map((s, i) => (
          <div
            key={s.key}
            title={`${signalLabel(s.key)}: ${s.pct.toFixed(0)}% (${PILLAR[s.pillar].label})`}
            style={{ width: `${(s.pct / total) * 100}%`, background: s.color }}
            className={i > 0 ? "border-l-2 border-white dark:border-slate-900" : ""}
          />
        ))}
      </div>
      {showLegend && (
        <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-slate-600 dark:text-slate-400">
          {segs.map((s) => (
            <span key={s.key} className="flex items-center gap-1">
              <span className="h-2 w-2 rounded-full" style={{ background: s.color }} />
              {signalLabel(s.key)}
              <span className="tabular-nums text-slate-500">{s.pct.toFixed(0)}%</span>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
