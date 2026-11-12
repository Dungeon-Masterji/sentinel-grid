"use client";

import useSWR from "swr";
import { Area, AreaChart, ResponsiveContainer, Tooltip, YAxis } from "recharts";
import { Activity } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { fetcher } from "@/lib/api";

type DayRow = {
  date: string;
  respiratory_encounter_count: number;
  heat_related_encounter_count: number;
  cardiometabolic_event_count: number;
  any_encounter_count: number;
};

type AggregateResponse = {
  since: string;
  until: string;
  latest_data_day: string;
  days: DayRow[];
  totals: Record<string, number>;
};

type SummaryResponse = {
  total_patients: number;
  generated_at: string | null;
};

const SPARK_COLOR = "#14b8a6"; // teal-500, matches the pillar icon

function SparkTooltip({ active, payload }: { active?: boolean; payload?: { payload: DayRow }[] }) {
  if (!active || !payload?.length) return null;
  const d = payload[0].payload;
  return (
    <div className="rounded-md border border-slate-200 bg-white px-2 py-1 text-[11px] shadow-sm dark:border-slate-700 dark:bg-slate-900">
      <div className="text-slate-500">{d.date}</div>
      <div className="font-medium text-slate-900 dark:text-slate-100">
        {d.respiratory_encounter_count} respiratory
      </div>
    </div>
  );
}

export function ClinicalCard() {
  // No params: the API returns the 7 days ending on the latest day with data.
  const agg = useSWR<AggregateResponse>("/api/signals/clinical/aggregate", fetcher, {
    refreshInterval: 300_000,
  });
  const summary = useSWR<SummaryResponse>("/api/signals/clinical/patients/summary", fetcher, {
    refreshInterval: 300_000,
  });

  const days = agg.data?.days ?? [];
  const today = days.at(-1);
  const live = Boolean(summary.data && today);

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between pb-2">
        <CardTitle className="text-sm font-medium text-slate-600 dark:text-slate-400">Clinical</CardTitle>
        <Activity className="h-4 w-4 text-teal-500" />
      </CardHeader>
      <CardContent>
        <div className="text-2xl font-semibold">{live ? "live" : "wiring"}</div>
        <p className="text-xs text-slate-500 mt-1">Synthea · FHIR bundles · synthetic</p>

        {live && today && summary.data && (
          <>
            <div className="mt-3 grid grid-cols-3 gap-2 text-xs">
              <div>
                <div className="text-slate-500">Patients</div>
                <div className="font-medium tabular-nums">{summary.data.total_patients.toLocaleString()}</div>
              </div>
              <div>
                <div className="text-slate-500">Respiratory</div>
                <div className="font-medium tabular-nums">{today.respiratory_encounter_count}</div>
              </div>
              <div>
                <div className="text-slate-500">Heat-related</div>
                <div className="font-medium tabular-nums">{today.heat_related_encounter_count}</div>
              </div>
            </div>
            <div className="mt-3 h-12" aria-label="Respiratory encounters, last 7 days">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={days} margin={{ top: 4, right: 2, bottom: 0, left: 2 }}>
                  <YAxis hide domain={[0, "dataMax"]} />
                  <Tooltip content={<SparkTooltip />} cursor={{ stroke: "#94a3b8", strokeWidth: 1 }} />
                  <Area
                    type="monotone"
                    dataKey="respiratory_encounter_count"
                    stroke={SPARK_COLOR}
                    strokeWidth={2}
                    fill={SPARK_COLOR}
                    fillOpacity={0.12}
                    dot={false}
                    activeDot={{ r: 4, strokeWidth: 2, stroke: "var(--background, #fff)" }}
                    isAnimationActive={false}
                  />
                </AreaChart>
              </ResponsiveContainer>
            </div>
            <p className="text-[11px] text-slate-500 mt-1">
              Respiratory encounters, 7 days to {today.date} (UTC)
            </p>
          </>
        )}
        {(agg.error || summary.error) && !live && (
          <p className="text-xs text-slate-500 mt-2">Clinical data not available yet</p>
        )}
      </CardContent>
    </Card>
  );
}
