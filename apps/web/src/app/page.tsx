"use client";

import useSWR from "swr";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Separator } from "@/components/ui/separator";
import { CloudSun, HardHat, AlertTriangle, MapPin } from "lucide-react";
import { ClinicalCard } from "@/components/clinical-card";
import { fetcher } from "@/lib/api";

type WaqiReading = {
  station_id: number | string;
  station_name: string;
  aqi: number | null;
  pm25: number | null;
  pm10: number | null;
  no2: number | null;
  o3: number | null;
  t_c: number | null;
  h_pct: number | null;
  time: string;
  coords: [number, number];
};

type AlertItem = {
  id: string;
  severity: "watch" | "advisory" | "warning";
  scenario: string;
  location: string;
  drivers: Record<string, number>;
  recommended_action: string;
  owner: string;
};

type AlertsResponse = { alerts: AlertItem[] };

function aqiBand(aqi: number | null) {
  if (aqi == null) return { label: "—", tone: "bg-slate-200 text-slate-700" };
  if (aqi <= 50) return { label: "Good", tone: "bg-emerald-100 text-emerald-800" };
  if (aqi <= 100) return { label: "Moderate", tone: "bg-amber-100 text-amber-800" };
  if (aqi <= 150) return { label: "Unhealthy (sensitive)", tone: "bg-orange-100 text-orange-800" };
  if (aqi <= 200) return { label: "Unhealthy", tone: "bg-rose-100 text-rose-800" };
  return { label: "Very unhealthy", tone: "bg-rose-200 text-rose-900" };
}

function StationCard({ city, lat, lon }: { city: string; lat: number; lon: number }) {
  const { data, error, isLoading } = useSWR<WaqiReading>(
    `/api/signals/env/waqi/nearest?lat=${lat}&lon=${lon}`,
    fetcher,
    { refreshInterval: 60_000 }
  );

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-start justify-between">
          <div>
            <CardTitle className="text-sm font-medium text-slate-600 dark:text-slate-400">{city}</CardTitle>
            <div className="flex items-center gap-1 mt-1 text-xs text-slate-500">
              <MapPin className="h-3 w-3" />
              {data?.station_name ?? "locating..."}
            </div>
          </div>
          {data && <Badge className={aqiBand(data.aqi).tone}>{aqiBand(data.aqi).label}</Badge>}
        </div>
      </CardHeader>
      <CardContent>
        {isLoading && <div className="text-2xl font-semibold text-slate-400">—</div>}
        {error && <div className="text-sm text-rose-600">API unreachable</div>}
        {data && (
          <>
            <div className="text-3xl font-semibold">{data.aqi ?? "—"}</div>
            <div className="text-xs text-slate-500 mt-1">AQI · {data.time}</div>
            <Separator className="my-3" />
            <div className="grid grid-cols-4 gap-2 text-xs">
              <div>
                <div className="text-slate-500">PM2.5</div>
                <div className="font-medium">{data.pm25 ?? "—"}</div>
              </div>
              <div>
                <div className="text-slate-500">PM10</div>
                <div className="font-medium">{data.pm10 ?? "—"}</div>
              </div>
              <div>
                <div className="text-slate-500">Temp</div>
                <div className="font-medium">{data.t_c ?? "—"}°C</div>
              </div>
              <div>
                <div className="text-slate-500">Humidity</div>
                <div className="font-medium">{data.h_pct ?? "—"}%</div>
              </div>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}

function AlertsPanel() {
  const { data, error, isLoading } = useSWR<AlertsResponse>("/api/alerts", fetcher, {
    refreshInterval: 30_000,
  });

  if (isLoading) return <div className="text-sm text-slate-500">Loading alerts…</div>;
  if (error) return <div className="text-sm text-rose-600">Alert feed unreachable</div>;
  if (!data?.alerts?.length) return <div className="text-sm text-slate-500">No active alerts.</div>;

  return (
    <div className="space-y-3">
      {data.alerts.map((a) => (
        <Alert key={a.id}>
          <AlertTriangle className="h-4 w-4" />
          <AlertTitle className="flex items-center gap-2">
            <Badge variant="outline" className="uppercase text-[10px]">{a.severity}</Badge>
            <span>{a.scenario}</span>
            <span className="text-slate-500 font-normal">· {a.location}</span>
          </AlertTitle>
          <AlertDescription>
            <div className="mt-2 text-sm">{a.recommended_action}</div>
            <div className="mt-2 text-xs text-slate-500">
              Owner: <span className="font-medium">{a.owner}</span>
              {"  ·  "}
              Drivers:{" "}
              {Object.entries(a.drivers).map(([k, v], i, arr) => (
                <span key={k}>
                  {k} {(v * 100).toFixed(0)}%{i < arr.length - 1 ? ", " : ""}
                </span>
              ))}
            </div>
          </AlertDescription>
        </Alert>
      ))}
    </div>
  );
}

export default function Home() {
  return (
    <main className="min-h-screen bg-slate-50 dark:bg-slate-950">
      <header className="border-b border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900">
        <div className="max-w-7xl mx-auto px-6 py-4 flex items-center justify-between">
          <div>
            <h1 className="text-xl font-semibold text-slate-900 dark:text-slate-100">Sentinel Grid</h1>
            <p className="text-sm text-slate-500">Tri-signal early warning · UAE</p>
          </div>
          <Badge variant="outline" className="gap-1.5">
            <span className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse" />
            Live
          </Badge>
        </div>
      </header>

      <div className="max-w-7xl mx-auto px-6 py-8 space-y-8">
        <section>
          <h2 className="text-sm uppercase tracking-wider text-slate-500 mb-3">Signal pillars</h2>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <ClinicalCard />

            <Card>
              <CardHeader className="flex flex-row items-center justify-between pb-2">
                <CardTitle className="text-sm font-medium text-slate-600 dark:text-slate-400">Environmental</CardTitle>
                <CloudSun className="h-4 w-4 text-amber-500" />
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-semibold">live</div>
                <p className="text-xs text-slate-500 mt-1">WAQI · Open-Meteo · CAMS</p>
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="flex flex-row items-center justify-between pb-2">
                <CardTitle className="text-sm font-medium text-slate-600 dark:text-slate-400">Occupational</CardTitle>
                <HardHat className="h-4 w-4 text-rose-500" />
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-semibold">wiring</div>
                <p className="text-xs text-slate-500 mt-1">Synthetic rosters · 15 sites</p>
              </CardContent>
            </Card>
          </div>
        </section>

        <section>
          <h2 className="text-sm uppercase tracking-wider text-slate-500 mb-3">Live air quality</h2>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <StationCard city="Dubai" lat={25.25} lon={55.35} />
            <StationCard city="Abu Dhabi" lat={24.47} lon={54.37} />
          </div>
        </section>

        <section>
          <h2 className="text-sm uppercase tracking-wider text-slate-500 mb-3">Active alerts</h2>
          <AlertsPanel />
        </section>
      </div>
    </main>
  );
}
