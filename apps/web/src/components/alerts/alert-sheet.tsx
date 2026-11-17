"use client";

import dynamic from "next/dynamic";
import { useState } from "react";
import { format, formatDistanceToNow } from "date-fns";
import { Area, ComposedChart, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { ShapWhy, type Lang } from "@/components/alerts/shap-why";
import { AckButton, ActionCard } from "@/components/alerts/action-card";
import {
  PILLAR,
  SEVERITY,
  signalLabel,
  useAlertDetail,
  type AlertItem,
  type TracePoint,
} from "@/lib/alerts";

const AlertHexMap = dynamic(() => import("@/components/alerts/alert-hex-map"), {
  ssr: false,
  loading: () => <Skeleton className="h-56 w-full rounded-lg" />,
});

const SEV_HEX = { watch: "#f59e0b", advisory: "#f97316", warning: "#e11d48" } as const;
const hhmm = (ts: string) => format(new Date(ts), "HH:mm");

function SignalTrace({ name, points, color, unit }: { name: string; points: TracePoint[]; color: string; unit: string | null }) {
  if (!points.length) {
    return <div className="text-xs text-slate-500">{signalLabel(name)}: no data (suppressed or missing)</div>;
  }
  return (
    <div>
      <div className="flex items-baseline justify-between text-xs">
        <span className="font-medium">{signalLabel(name)}</span>
        <span className="text-slate-500">
          {unit} · <span style={{ color }}>■</span> value <span className="text-slate-400">┄ baseline</span>
        </span>
      </div>
      <div className="h-20">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={points} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
            <XAxis dataKey="ts" tickFormatter={hhmm} tick={{ fontSize: 10 }} minTickGap={40} stroke="#94a3b8" />
            <YAxis tick={{ fontSize: 10 }} width={34} stroke="#94a3b8" domain={["auto", "auto"]} />
            <Tooltip
              labelFormatter={(l) => format(new Date(String(l)), "d MMM HH:mm")}
              formatter={(v, k) => [typeof v === "number" ? v.toFixed(2) : String(v), String(k)]}
              contentStyle={{ fontSize: 11 }}
            />
            <Line type="monotone" dataKey="baseline" stroke="#94a3b8" strokeDasharray="4 3" dot={false} strokeWidth={1.25} isAnimationActive={false} />
            <Line type="monotone" dataKey="value" stroke={color} dot={false} strokeWidth={2} isAnimationActive={false} />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

export function AlertSheet({ alert, onOpenChange }: { alert: AlertItem | null; onOpenChange: (o: boolean) => void }) {
  const { data, isLoading, mutate } = useAlertDetail(alert?.id ?? null);
  const d = data?.alert;
  const sev = alert ? SEVERITY[alert.severity] : null;
  const [lang, setLang] = useState<Lang>("en");
  // detail (with regenerate_narration overrides) wins over the list item once loaded
  const shown = d ?? alert;

  return (
    <Sheet open={alert != null} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full overflow-y-auto sm:max-w-xl">
        {alert && shown && sev && (
          // Arabic flips the whole panel; charts mirror themselves via `lang`
          <div dir={lang === "ar" ? "rtl" : "ltr"} lang={lang} className="contents">
            <SheetHeader className="pr-12">
              <div className="flex items-center gap-2">
                <Badge className={sev.badge}>{sev.label}</Badge>
                <span className="text-xs text-slate-500">
                  {formatDistanceToNow(new Date(alert.created_at), { addSuffix: true })}
                </span>
              </div>
              <SheetTitle className="text-lg">{alert.scenario_label}</SheetTitle>
              <SheetDescription>
                {alert.site_names.join(", ")} · {alert.emirates.join(", ")} · clinical catchment{" "}
                {alert.clinical_catchment}
              </SheetDescription>
            </SheetHeader>

            <div className="space-y-5 px-4 pb-6">
              <div className="grid grid-cols-3 gap-2 text-xs">
                <div>
                  <div className="text-slate-500">Fused score</div>
                  <div className="text-base font-semibold tabular-nums">{alert.score.toFixed(2)}</div>
                </div>
                <div>
                  <div className="text-slate-500">90% credible interval</div>
                  <div dir="ltr" className="font-medium tabular-nums rtl:text-right">
                    {alert.credible_interval[0].toFixed(2)} – {alert.credible_interval[1].toFixed(2)}
                  </div>
                </div>
                <div>
                  <div className="text-slate-500">Evidence coverage</div>
                  <div className="font-medium tabular-nums">{Math.round(alert.coverage * 100)}%</div>
                </div>
              </div>

              <ShapWhy alert={shown} detail={d} lang={lang} onLang={setLang} />

              <section className="space-y-2">
                <ActionCard alert={shown} lang={lang} />
                <AckButton key={alert.id} alert={shown} lang={lang} onDone={() => mutate()} />
              </section>

              <section>
                <h3 className="mb-2 text-xs uppercase tracking-wider text-slate-500">Signal readings</h3>
                <table className="mt-3 w-full text-xs">
                  <thead className="text-slate-500">
                    <tr className="text-left">
                      <th className="font-normal">Signal</th>
                      <th className="font-normal text-right">Value</th>
                      <th className="font-normal text-right">Baseline</th>
                      <th className="font-normal text-right">z</th>
                      <th className="font-normal text-right">Score</th>
                      <th className="font-normal text-right">Weight</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(alert.driver_details).map(([k, v]) => (
                      <tr key={k} className="border-t border-slate-100 dark:border-slate-800">
                        <td className="py-1">
                          {signalLabel(k)}
                          <div className="text-[10px] text-slate-500">{v.source}</div>
                        </td>
                        <td dir="ltr" className="text-right tabular-nums">{v.suppressed || v.value == null ? "—" : v.value.toFixed(2)}</td>
                        <td dir="ltr" className="text-right tabular-nums">{v.baseline == null ? "—" : v.baseline.toFixed(2)}</td>
                        <td dir="ltr" className="text-right tabular-nums">{v.z == null ? "—" : v.z.toFixed(1)}</td>
                        <td dir="ltr" className="text-right tabular-nums">{v.score == null ? "—" : v.score.toFixed(2)}</td>
                        <td dir="ltr" className="text-right tabular-nums">{v.weight.toFixed(2)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </section>

              <section>
                <h3 className="mb-2 text-xs uppercase tracking-wider text-slate-500">Affected area · H3 {alert.h3_cell}</h3>
                {d ? (
                  <AlertHexMap footprint={d.footprint} color={SEV_HEX[alert.severity]} />
                ) : (
                  <Skeleton className="h-56 w-full rounded-lg" />
                )}
              </section>

              <section className="space-y-3">
                <h3 className="text-xs uppercase tracking-wider text-slate-500">Last 24 h to peak (Asia/Dubai)</h3>
                {isLoading && <Skeleton className="h-40 w-full" />}
                {d && d.traces.fused.length > 0 && (
                  <div>
                    <div className="text-xs font-medium">Fused score · 90% interval</div>
                    <div className="h-20">
                      <ResponsiveContainer width="100%" height="100%">
                        <ComposedChart data={d.traces.fused} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
                          <XAxis dataKey="ts" tickFormatter={hhmm} tick={{ fontSize: 10 }} minTickGap={40} stroke="#94a3b8" />
                          <YAxis tick={{ fontSize: 10 }} width={34} domain={[0, 1]} stroke="#94a3b8" />
                          <Tooltip labelFormatter={(l) => format(new Date(String(l)), "d MMM HH:mm")} contentStyle={{ fontSize: 11 }} />
                          <Area dataKey={(p: { ci_low: number; ci_high: number }) => [p.ci_low, p.ci_high]} stroke="none" fill={SEV_HEX[alert.severity]} fillOpacity={0.15} isAnimationActive={false} name="90% CI" />
                          <Line type="monotone" dataKey="mean" stroke={SEV_HEX[alert.severity]} dot={false} strokeWidth={2} isAnimationActive={false} name="fused" />
                        </ComposedChart>
                      </ResponsiveContainer>
                    </div>
                  </div>
                )}
                {d &&
                  Object.entries(d.traces.signals).map(([k, pts]) => {
                    const det = alert.driver_details[k];
                    const color = PILLAR[det?.pillar ?? "env"].hex;
                    return <SignalTrace key={k} name={k} points={pts} color={color} unit={det?.unit ?? null} />;
                  })}
              </section>

              <section>
                <h3 className="mb-1 text-xs uppercase tracking-wider text-slate-500">Playbook actions</h3>
                <div className="mb-2 text-xs text-slate-500">
                  Owner: <span className="font-medium text-slate-700 dark:text-slate-300">{alert.action_owner}</span>
                </div>
                <ul className="list-disc space-y-1 pl-5 text-sm">
                  {alert.recommended_actions.map((a) => (
                    <li key={a}>{a}</li>
                  ))}
                </ul>
              </section>

              <Separator />

              <section className="space-y-2">
                <h3 className="text-xs uppercase tracking-wider text-slate-500">Acknowledgements</h3>
                {d?.acks?.length ? (
                  <ul className="space-y-1 text-xs">
                    {d.acks.map((a, i) => (
                      <li key={i}>
                        <span className="font-medium">{a.acknowledged_by}</span>{" "}
                        <span className="text-slate-500">
                          · {formatDistanceToNow(new Date(a.acknowledged_at), { addSuffix: true })}
                        </span>
                        {a.notes && <div className="text-slate-600 dark:text-slate-400">{a.notes}</div>}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <div className="text-xs text-slate-500">Not acknowledged yet.</div>
                )}
              </section>
            </div>
          </div>
        )}
      </SheetContent>
    </Sheet>
  );
}
