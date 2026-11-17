import { EVENT_TYPE, SCENARIO, type Scorecard } from "@/lib/backtest";

/** Sequential one-hue ramp (light -> dark on light surface; selected dark steps in .dark). */
const STEPS = ["var(--bt-seq-0)", "var(--bt-seq-1)", "var(--bt-seq-2)", "var(--bt-seq-3)", "var(--bt-seq-4)", "var(--bt-seq-5)"];

function fill(v: number) {
  return STEPS[Math.min(STEPS.length - 1, Math.round(v * (STEPS.length - 1)))];
}

/** Scenario x event-type recall. Cells with no events of that type say so instead of
 *  showing a misleading 0%. Values are printed in text ink on a chip, never in the fill. */
export function CoverageHeatmap({ card }: { card: Scorecard }) {
  const cm = card.coverage_matrix;
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[560px] border-separate border-spacing-[2px] text-xs">
        <thead>
          <tr>
            <th className="w-40 text-left font-normal text-muted-foreground">scenario ↓ · event type →</th>
            {cm.event_types.map((t) => (
              <th key={t} className="px-1 pb-1 text-center font-medium">
                {EVENT_TYPE[t] ?? t}
                <div className="font-normal text-muted-foreground">n={cm.n_events[t] ?? 0}</div>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {cm.scenarios.map((s, i) => (
            <tr key={s}>
              <th className="pr-2 text-left font-medium">{SCENARIO[s]?.label ?? s}</th>
              {cm.values[i].map((v, j) => {
                const t = cm.event_types[j];
                const n = cm.n_events[t] ?? 0;
                const tip = v == null
                  ? `${SCENARIO[s]?.label ?? s} × ${EVENT_TYPE[t] ?? t}: no ground-truth events of this type`
                  : `${SCENARIO[s]?.label ?? s} caught ${Math.round(v * n)} of ${n} ${EVENT_TYPE[t] ?? t} event(s)`;
                return (
                  <td
                    key={t}
                    title={tip}
                    className="h-12 rounded-md text-center transition-[outline] hover:outline hover:outline-2 hover:outline-foreground/40"
                    style={v == null
                      ? { background: "repeating-linear-gradient(45deg, var(--muted) 0 4px, transparent 4px 8px)" }
                      : { background: fill(v) }}
                  >
                    <span className="rounded bg-background/85 px-1.5 py-0.5 tabular-nums text-foreground">
                      {v == null ? "no events" : `${Math.round(v * 100)}%`}
                    </span>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
      <div className="mt-2 flex items-center gap-2 text-[11px] text-muted-foreground">
        recall 0%
        <span className="flex h-2.5 w-32 overflow-hidden rounded-sm">
          {STEPS.map((c) => <span key={c} className="flex-1" style={{ background: c }} />)}
        </span>
        100% · every scenario is scored against every type, expected or not
      </div>
    </div>
  );
}
