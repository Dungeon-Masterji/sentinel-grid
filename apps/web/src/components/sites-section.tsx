"use client";

import dynamic from "next/dynamic";
import { Area, AreaChart, ResponsiveContainer, Tooltip, YAxis } from "recharts";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { TIER, pct, useOccupationalSites, type SiteStatus } from "@/lib/occupational";

// maplibre touches window/WebGL: client-only.
const SitesMap = dynamic(() => import("@/components/sites-map"), {
  ssr: false,
  loading: () => <Skeleton className="h-[440px] w-full rounded-xl" />,
});

type Point = { date: string; worked_hours: number | null };

function SparkTooltip({ active, payload }: { active?: boolean; payload?: { payload: Point }[] }) {
  if (!active || !payload?.length) return null;
  const d = payload[0].payload;
  return (
    <div className="rounded-md border border-slate-200 bg-white px-2 py-1 text-[11px] shadow-sm dark:border-slate-700 dark:bg-slate-900">
      <div className="text-slate-500">{d.date}</div>
      <div className="font-medium text-slate-900 dark:text-slate-100">
        {d.worked_hours?.toLocaleString() ?? "—"} h worked
      </div>
    </div>
  );
}

function SiteCard({ s }: { s: SiteStatus }) {
  const tier = TIER[s.risk_tier];
  return (
    <Card size="sm">
      <CardHeader className="pb-0">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <CardTitle className="text-sm font-medium leading-snug">{s.name}</CardTitle>
            <div className="mt-0.5 text-xs text-slate-500 capitalize">
              {s.sector} · {s.emirate} · {s.heat_exposure.replace("_", " ")}
            </div>
          </div>
          <Badge className={tier.badge}>{tier.label}</Badge>
        </div>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-3 gap-2 text-xs">
          <div>
            <div className="text-slate-500">Workers</div>
            <div className="font-medium tabular-nums">{s.worker_count.toLocaleString()}</div>
          </div>
          <div>
            <div className="text-slate-500">Absence</div>
            <div className="font-medium tabular-nums">{pct(s.absence_rate)}</div>
          </div>
          <div>
            <div className="text-slate-500">Feels-like</div>
            <div className="font-medium tabular-nums">
              {s.max_apparent_temp_today == null ? "—" : `${s.max_apparent_temp_today.toFixed(0)}°C`}
            </div>
          </div>
        </div>
        <div className="mt-2 h-10" aria-label={`Worked hours, last 7 days, ${s.name}`}>
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={s.worked_hours_7d} margin={{ top: 3, right: 2, bottom: 0, left: 2 }}>
              <YAxis hide domain={[0, "dataMax"]} />
              <Tooltip content={<SparkTooltip />} cursor={{ stroke: "#94a3b8", strokeWidth: 1 }} />
              <Area
                type="monotone"
                dataKey="worked_hours"
                stroke={tier.hex}
                strokeWidth={1.75}
                fill={tier.hex}
                fillOpacity={0.12}
                dot={false}
                activeDot={{ r: 3, strokeWidth: 2, stroke: "var(--background, #fff)" }}
                isAnimationActive={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
        <p className="text-[11px] text-slate-500">Worked hours · 7 days</p>
      </CardContent>
    </Card>
  );
}

export function SitesSection() {
  const { data, error, isLoading } = useOccupationalSites();

  return (
    <section className="space-y-4">
      <div className="flex items-baseline justify-between">
        <h2 className="text-sm uppercase tracking-wider text-slate-500">Sites</h2>
        {data && (
          <span className="text-xs text-slate-500">
            Rota day {data.current_day} (Asia/Dubai) · synthetic workforce
          </span>
        )}
      </div>
      {isLoading && (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-40 rounded-xl" />
          ))}
        </div>
      )}
      {error && !data && <div className="text-sm text-slate-500">Site data not available yet.</div>}
      {data && (
        <>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            {data.sites.map((s) => (
              <SiteCard key={s.site_id} s={s} />
            ))}
          </div>
          <SitesMap sites={data.sites} />
        </>
      )}
    </section>
  );
}
