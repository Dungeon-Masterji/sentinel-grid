"use client";

import { useState } from "react";
import { format } from "date-fns";
import { CheckCircle2, ChevronDown, ChevronRight, ExternalLink, XCircle } from "lucide-react";
import {
  CartesianGrid, ComposedChart, Legend, Line, ReferenceArea, ReferenceLine, ResponsiveContainer, Scatter, Tooltip, XAxis, YAxis,
} from "recharts";
import { Skeleton } from "@/components/ui/skeleton";
import { PILLAR, signalLabel, type Pillar } from "@/lib/alerts";
import { EVENT_TYPE, SCENARIO, useReplay, type BacktestEvent, type ReplayResponse } from "@/lib/backtest";

const MUTED = "var(--muted-foreground)";
const PILLAR_OF: Record<string, Pillar> = {
  apparent_temp: "env", pm25: "env", pm10: "env",
  absence_rate: "occ", respiratory_absence_rate: "occ", heat_absence_rate: "occ",
  heat_related_encounter_count: "clin", respiratory_encounter_count: "clin",
  any_encounter_count: "clin", fever_encounter_count: "clin",
};
const fmtT = (ms: number) => format(new Date(ms), "d MMM HH:mm");

function leadText(e: BacktestEvent) {
  if (!e.detected || e.lead_time_hours == null) return "missed";
  const h = e.lead_time_hours;
  return h >= 0 ? `${h.toFixed(1)} h before start` : `${Math.abs(h).toFixed(1)} h after start`;
}

function Bands({ ev }: { ev: BacktestEvent }) {
  const s = Date.parse(ev.start), e = Date.parse(ev.end), p = Date.parse(ev.peak);
  return (
    <>
      <ReferenceArea x1={s} x2={e} fill="var(--bt-event-band)" ifOverflow="extendDomain" />
      <ReferenceLine x={s} stroke={MUTED} strokeDasharray="3 3" label={{ value: "start", position: "top", fontSize: 10, fill: MUTED }} />
      <ReferenceLine x={p} stroke="var(--foreground)" strokeDasharray="1 3" label={{ value: "peak", position: "top", fontSize: 10, fill: MUTED }} />
      <ReferenceLine x={e} stroke={MUTED} strokeDasharray="3 3" label={{ value: "end", position: "top", fontSize: 10, fill: MUTED }} />
    </>
  );
}

/** One tick per local midnight (every 2/4/7 days on longer windows): no repeated labels. */
function dayTicks(lo: number, hi: number): number[] {
  const days = (hi - lo) / 86_400_000;
  const step = days > 40 ? 7 : days > 20 ? 4 : days > 10 ? 2 : 1;
  const d = new Date(lo);
  d.setHours(0, 0, 0, 0);
  if (d.getTime() < lo) d.setDate(d.getDate() + 1);
  const out: number[] = [];
  for (let i = 0; d.getTime() <= hi; i++, d.setDate(d.getDate() + 1)) if (i % step === 0) out.push(d.getTime());
  return out;
}

const xAxis = (lo: number, hi: number) => (
  <XAxis type="number" dataKey="t" domain={[lo, hi]} scale="time" ticks={dayTicks(lo, hi)}
    tickFormatter={(v: number) => format(new Date(v), "d MMM")} tick={{ fontSize: 10, fill: MUTED }} stroke="var(--border)" />
);

function Traces({ data }: { data: ReplayResponse }) {
  const ev = data.event;
  const rows = data.hours.map((h) => ({ t: Date.parse(h.ts), ...h.signals, ...Object.fromEntries(Object.entries(h.fused).map(([k, v]) => [`f:${k}`, v])) }));
  if (!rows.length) return <div className="text-xs text-muted-foreground">No traces for this event.</div>;
  const lo = rows[0].t, hi = rows[rows.length - 1].t;
  const signals = Object.keys(data.hours[0]?.signals ?? {}).filter((k) => rows.some((r) => (r as Record<string, unknown>)[k] != null));
  const scenarios = Object.keys(SCENARIO).filter((s) => rows.some((r) => (r as Record<string, unknown>)[`f:${s}`] != null));
  // alert markers live in the chart's own rows (a separate Scatter data array would give
  // recharts a second x-domain): at the hour the alert was issued, at that scenario's score
  const byTime = new Map(rows.map((r) => [r.t, r as Record<string, number | null>]));
  for (const a of ev.alerts) {
    const r = byTime.get(Math.floor(Date.parse(a.first_seen) / 3_600_000) * 3_600_000);
    if (r) r[`m:${a.scenario_id}`] = r[`f:${a.scenario_id}`] ?? a.score_max;
  }
  return (
    <div className="space-y-3">
      <div>
        <div className="mb-1 text-xs font-medium">Fused score per scenario (max over the event&apos;s hexes) · ◆ = alert issued</div>
        <div className="h-48">
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={rows} margin={{ top: 16, right: 8, bottom: 0, left: 0 }}>
              <CartesianGrid stroke="var(--border)" strokeOpacity={0.5} vertical={false} />
              {xAxis(lo, hi)}
              <YAxis domain={[0, 1]} tick={{ fontSize: 10, fill: MUTED }} stroke="var(--border)" width={30} />
              <Bands ev={ev} />
              <Tooltip labelFormatter={(v) => fmtT(Number(v))} contentStyle={{ fontSize: 11 }}
                formatter={(v, k) => [typeof v === "number" ? v.toFixed(3) : String(v),
                  (String(k).startsWith("m:") ? "alert issued · " : "") + (SCENARIO[String(k).slice(2)]?.label ?? String(k))]} />
              <Legend wrapperStyle={{ fontSize: 11 }} formatter={(k) => SCENARIO[String(k).slice(2)]?.label ?? k} />
              {scenarios.map((s) => (
                <Line key={s} dataKey={`f:${s}`} name={`f:${s}`} stroke={SCENARIO[s].color} strokeWidth={ev.expected_scenarios.includes(s) ? 2 : 1.25}
                  strokeOpacity={ev.expected_scenarios.includes(s) ? 1 : 0.55} dot={false} isAnimationActive={false} connectNulls={false} />
              ))}
              {scenarios.map((s) => (
                <Scatter key={`m-${s}`} dataKey={`m:${s}`} legendType="none" name={`m:${s}`}
                  fill={SCENARIO[s].color} stroke="var(--popover)" strokeWidth={2} shape="diamond" isAnimationActive={false} />
              ))}
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      </div>
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        {signals.map((k) => (
          <div key={k}>
            <div className="text-xs font-medium">{signalLabel(k)}</div>
            <div className="h-24">
              <ResponsiveContainer width="100%" height="100%">
                <ComposedChart data={rows} margin={{ top: 12, right: 8, bottom: 0, left: 0 }}>
                  {xAxis(lo, hi)}
                  <YAxis tick={{ fontSize: 10, fill: MUTED }} stroke="var(--border)" width={36} domain={["auto", "auto"]} />
                  <Bands ev={ev} />
                  <Tooltip labelFormatter={(v) => fmtT(Number(v))} contentStyle={{ fontSize: 11 }}
                    formatter={(v) => [typeof v === "number" ? v.toFixed(2) : String(v), signalLabel(k)]} />
                  <Line dataKey={k} stroke={PILLAR[PILLAR_OF[k] ?? "env"].hex} strokeWidth={2} dot={false} isAnimationActive={false} />
                </ComposedChart>
              </ResponsiveContainer>
            </div>
          </div>
        ))}
      </div>
      <p className="text-[11px] text-muted-foreground">
        Shaded band = event window; clinical traces are lagged 48 h (the T-2 rule), so they trail the weather.
      </p>
    </div>
  );
}

function EventRow({ ev }: { ev: BacktestEvent }) {
  const [open, setOpen] = useState(false);
  const { data, isLoading } = useReplay(open ? ev.id : null);
  return (
    <li className="rounded-lg border">
      <button type="button" onClick={() => setOpen((o) => !o)} aria-expanded={open}
        className="flex w-full items-center gap-3 px-3 py-2.5 text-left hover:bg-muted/50">
        {open ? <ChevronDown className="size-4 shrink-0" /> : <ChevronRight className="size-4 shrink-0" />}
        {ev.detected
          ? <span className="flex w-24 shrink-0 items-center gap-1 text-xs text-emerald-700 dark:text-emerald-400"><CheckCircle2 className="size-4" />Detected</span>
          : <span className="flex w-24 shrink-0 items-center gap-1 text-xs text-rose-700 dark:text-rose-400"><XCircle className="size-4" />Missed</span>}
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm font-medium">{ev.label}</span>
          <span className="text-xs text-muted-foreground">
            {EVENT_TYPE[ev.event_type] ?? ev.event_type} · {format(new Date(ev.start), "d MMM yyyy")} · {ev.affected_emirates.join(", ")}
          </span>
        </span>
        <span className="hidden text-right text-xs sm:block">
          <span className="block font-medium tabular-nums">{leadText(ev)}</span>
          <span className="text-muted-foreground">
            {ev.detected ? `first ${ev.alert_severity_at_first_hit} · peak ${ev.peak_severity_during_event}` : `expected: ${ev.expected_scenarios.map((s) => SCENARIO[s]?.label ?? s).join(", ")}`}
          </span>
        </span>
      </button>
      {open && (
        <div className="space-y-3 border-t px-3 py-3">
          <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 text-xs">
            <span>Lead time: <span className="font-semibold">{leadText(ev)}</span></span>
            <span className="text-muted-foreground">{ev.n_matching_alerts} matching alert(s)</span>
            {ev.peak_value && <span className="text-muted-foreground">peak {ev.peak_value}</span>}
            <a href={ev.source_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 underline underline-offset-2">
              source <ExternalLink className="size-3" />
            </a>
          </div>
          {ev.notes && <p className="text-xs text-muted-foreground">{ev.notes}</p>}
          {isLoading && <Skeleton className="h-64 w-full" />}
          {data && <Traces data={data} />}
        </div>
      )}
    </li>
  );
}

export function EventList({ events }: { events: BacktestEvent[] }) {
  const sorted = [...events].sort((a, b) => a.start.localeCompare(b.start));
  return <ul className="max-h-[70vh] space-y-2 overflow-y-auto pr-1">{sorted.map((e) => <EventRow key={e.id} ev={e} />)}</ul>;
}
