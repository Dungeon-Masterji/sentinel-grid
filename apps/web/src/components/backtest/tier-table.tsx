import type { Scorecard } from "@/lib/backtest";

const pct = (v: number | null | undefined) => (v == null ? "—" : `${Math.round(v * 100)}%`);
const TIER_LABEL = { watch: "Watch or higher", advisory: "Advisory or higher", warning: "Warning" } as const;

/** Each tier against randomly re-timed copies of its own alert stream (seasonal null:
 *  shifted +-7..45 days). Sensitivity near chance = uninformative; PPV lift > 1 = alerts
 *  concentrate in real event windows. */
export function TierTable({ card }: { card: Scorecard }) {
  const tiers = (["watch", "advisory", "warning"] as const).filter((t) => card.by_tier?.[t]);
  if (!tiers.length) return null;
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[640px] text-xs">
        <thead className="text-muted-foreground">
          <tr className="text-right">
            <th className="text-left font-normal">Alerts counted</th>
            <th className="font-normal">Alerts</th>
            <th className="font-normal">False alarms / wk</th>
            <th className="font-normal">Sensitivity</th>
            <th className="font-normal">…by chance</th>
            <th className="font-normal">PPV</th>
            <th className="font-normal">…by chance</th>
            <th className="font-normal">PPV lift</th>
            <th className="font-normal">Median lead</th>
          </tr>
        </thead>
        <tbody>
          {tiers.map((t) => {
            const b = card.by_tier[t];
            const c = b.chance_seasonal;
            const beats = b.ppv != null && c?.ppv_p95 != null && b.ppv > c.ppv_p95;
            return (
              <tr key={t} className="border-t text-right tabular-nums">
                <td className="py-1.5 text-left font-medium">{TIER_LABEL[t]}</td>
                <td>{b.alerts.toLocaleString()}</td>
                <td>{b.false_alarms_per_week ?? "—"}</td>
                <td>{pct(b.sensitivity)} <span className="text-muted-foreground">({b.detected}/{b.events})</span></td>
                <td className="text-muted-foreground">{pct(c?.sensitivity_mean)}</td>
                <td>{b.ppv == null ? "—" : `${(b.ppv * 100).toFixed(1)}%`}</td>
                <td className="text-muted-foreground">{c?.ppv_mean == null ? "—" : `${(c.ppv_mean * 100).toFixed(1)}%`}</td>
                <td className="font-medium">
                  {b.ppv_lift == null ? "—" : `${b.ppv_lift.toFixed(1)}×`}
                  <span className="ml-1 font-normal text-muted-foreground">{beats ? "> chance p95" : "within chance"}</span>
                </td>
                <td>{b.median_lead_time_hours == null ? "—" : `${b.median_lead_time_hours} h`}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="mt-2 text-[11px] text-muted-foreground">
        Chance = the same alerts, places and rhythm, shifted ±7–45 days at random ({card.by_tier.watch?.chance_seasonal?.n ?? 0}{" "}
        shuffles). Sensitivity that matches chance is driven by alert volume, not skill; PPV lift shows how much more often
        alerts land inside real event windows than chance would put them there.
      </p>
    </div>
  );
}
