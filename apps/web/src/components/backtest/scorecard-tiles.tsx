import { Card, CardContent } from "@/components/ui/card";
import type { Scorecard } from "@/lib/backtest";

const pct = (v: number | null | undefined) => (v == null ? "—" : `${Math.round(v * 100)}%`);
const num = (v: number | null | undefined, d = 1) => (v == null ? "—" : v.toFixed(d));

function Tile({ label, value, sub }: { label: string; value: string; sub: string }) {
  return (
    <Card size="sm">
      <CardContent>
        <div className="text-xs text-muted-foreground">{label}</div>
        <div className="mt-1 text-3xl font-semibold tabular-nums">{value}</div>
        <div className="mt-1 text-xs text-muted-foreground">{sub}</div>
      </CardContent>
    </Card>
  );
}

/** Headline numbers. Stat tiles, not charts: each is one number the reader needs. */
export function ScorecardTiles({ card }: { card: Scorecard }) {
  const o = card.overall;
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
      <Tile label="Sensitivity" value={pct(o.sensitivity)}
        sub={`${o.detected} of ${o.events} events · by chance ${pct(o.chance_seasonal?.sensitivity_mean)}`} />
      <Tile label="PPV (precision)" value={o.ppv == null ? "—" : `${(o.ppv * 100).toFixed(1)}%`}
        sub={`${o.matched_alerts.toLocaleString()} of ${o.alerts.toLocaleString()} alerts · ${o.ppv_lift == null ? "" : `${o.ppv_lift.toFixed(1)}× chance`}`} />
      <Tile
        label="Mean lead time"
        value={o.mean_lead_time_hours == null ? "—" : `${num(o.mean_lead_time_hours)} h`}
        sub={`median ${num(o.median_lead_time_hours)} h · ${o.detected_before_start} warned before start`}
      />
      <Tile label="False alarms / week" value={num(o.false_alarms_per_week)} sub={`${o.false_alarms.toLocaleString()} unmatched alerts over ${num(card.window.weeks, 0)} weeks`} />
      <Tile
        label="Brier score"
        value={num(o.brier, 3)}
        sub={`climatology ${num(o.brier_climatology, 3)} · skill ${num(o.brier_skill, 2)}`}
      />
    </div>
  );
}
