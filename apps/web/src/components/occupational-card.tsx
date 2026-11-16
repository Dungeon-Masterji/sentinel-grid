"use client";

import { HardHat } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { TIER, pct, useOccupationalSites } from "@/lib/occupational";

export function OccupationalCard() {
  const { data, error } = useOccupationalSites();
  const top = data ? [...data.sites].sort((a, b) => b.risk_score - a.risk_score).slice(0, 3) : [];

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between pb-2">
        <CardTitle className="text-sm font-medium text-slate-600 dark:text-slate-400">Occupational</CardTitle>
        <HardHat className="h-4 w-4 text-rose-500" />
      </CardHeader>
      <CardContent>
        <div className="text-2xl font-semibold">{data ? "live" : "wiring"}</div>
        <p className="text-xs text-slate-500 mt-1">
          Synthetic rosters · {data?.totals.sites ?? 15} sites
        </p>

        {data && (
          <>
            <div className="mt-3 grid grid-cols-2 gap-2 text-xs">
              <div>
                <div className="text-slate-500">Active workers today</div>
                <div className="font-medium tabular-nums">
                  {data.totals.active_today.toLocaleString()}
                  <span className="text-slate-400 font-normal">
                    {" "}/ {data.totals.roster.toLocaleString()}
                  </span>
                </div>
              </div>
              <div>
                <div className="text-slate-500">Sites at elevated risk</div>
                <div
                  className={`font-medium tabular-nums ${
                    data.totals.elevated_sites > 0 ? "text-rose-600" : ""
                  }`}
                >
                  {data.totals.elevated_sites}
                  <span className="text-slate-400 font-normal"> of {data.totals.sites}</span>
                </div>
              </div>
            </div>
            <div className="mt-3 text-[11px] uppercase tracking-wider text-slate-500">Highest risk</div>
            <ul className="mt-1 space-y-1 text-xs">
              {top.map((s) => (
                <li key={s.site_id} className="flex items-center gap-2">
                  <span
                    className={`h-2 w-2 shrink-0 rounded-full ${TIER[s.risk_tier].dot}`}
                    aria-label={TIER[s.risk_tier].label}
                  />
                  <span className="truncate">{s.name}</span>
                  <span className="ml-auto tabular-nums text-slate-500">{pct(s.absence_rate)}</span>
                </li>
              ))}
            </ul>
            <p className="text-[11px] text-slate-500 mt-2">
              Elevated = absence &gt; 5% or mean apparent temp &gt; 42 °C today
            </p>
          </>
        )}
        {error && !data && (
          <p className="text-xs text-slate-500 mt-2">Occupational data not available yet</p>
        )}
      </CardContent>
    </Card>
  );
}
