import useSWR from "swr";
import { API_BASE, fetcher } from "@/lib/api";

export type Severity = "watch" | "advisory" | "warning";
export type Pillar = "env" | "occ" | "clin";

export type DriverDetail = {
  pillar: Pillar;
  weight: number;
  unit: string | null;
  value: number | null;
  baseline: number | null;
  z: number | null;
  score: number | null;
  suppressed: boolean;
  source: string;
};

export type AlertItem = {
  id: string;
  scenario_id: string;
  scenario_label: string;
  severity: Severity;
  h3_cell: string;
  sites_affected: string[];
  site_names: string[];
  emirates: string[];
  clinical_catchment: string;
  score: number;
  credible_interval: [number, number];
  coverage: number;
  /** signal -> contribution %, sums to 100. Chunk 4 swaps in SHAP values; same shape. */
  drivers: Record<string, number>;
  driver_details: Record<string, DriverDetail>;
  created_at: string;
  peak_at: string;
  window_start: string;
  expires_at: string;
  as_of: string;
  action_owner: string;
  recommended_actions: string[];
};

export type TracePoint = { ts: string; value: number | null; baseline: number | null; score: number | null };
export type FusedPoint = { ts: string; mean: number | null; ci_low: number | null; ci_high: number | null };
export type Ack = { alert_id: string; acknowledged_by: string; notes: string; acknowledged_at: string };

export type AlertDetail = AlertItem & {
  footprint: GeoJSON.Feature<GeoJSON.Polygon>;
  traces: { signals: Record<string, TracePoint[]>; fused: FusedPoint[] };
  acks: Ack[];
  expired: boolean;
};

export type AlertsResponse = {
  notice: string;
  as_of: string | null;
  mode: string | null;
  count: number;
  alerts: AlertItem[];
  scenarios: string[];
};

export function useAlerts() {
  return useSWR<AlertsResponse>("/api/alerts", fetcher, { refreshInterval: 60_000 });
}

export function useAlertDetail(id: string | null) {
  return useSWR<{ alert: AlertDetail }>(id ? `/api/alerts/${id}` : null, fetcher);
}

export async function ackAlert(id: string, acknowledged_by: string, notes: string) {
  const r = await fetch(`${API_BASE}/api/alerts/${id}/ack`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ acknowledged_by, notes }),
  });
  if (!r.ok) throw new Error(`ack failed (${r.status})`);
  return r.json();
}

export const SEVERITY: Record<Severity, { label: string; badge: string; rail: string; rank: number }> = {
  watch: {
    label: "Watch",
    badge: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300",
    rail: "bg-amber-400",
    rank: 0,
  },
  advisory: {
    label: "Advisory",
    badge: "bg-orange-100 text-orange-800 dark:bg-orange-950 dark:text-orange-300",
    rail: "bg-orange-500",
    rank: 1,
  },
  warning: {
    label: "Warning",
    badge: "bg-rose-100 text-rose-800 dark:bg-rose-950 dark:text-rose-300",
    rail: "bg-rose-600",
    rank: 2,
  },
};

/** Pillar hues match the pillar card icons (teal clinical, amber env, rose occupational). */
export const PILLAR: Record<Pillar, { label: string; shades: string[] }> = {
  env: { label: "Environmental", shades: ["#d97706", "#f59e0b", "#fbbf24"] },
  occ: { label: "Occupational", shades: ["#e11d48", "#fb7185"] },
  clin: { label: "Clinical", shades: ["#0d9488", "#2dd4bf"] },
};

export const SIGNAL_LABEL: Record<string, string> = {
  apparent_temp: "Apparent temp",
  pm25: "PM2.5",
  pm10: "PM10",
  absence_rate: "Worker absence",
  respiratory_absence_rate: "Resp. absence",
  heat_related_encounter_count: "Heat encounters",
  respiratory_encounter_count: "Resp. encounters",
  any_encounter_count: "All encounters",
  fever_encounter_count: "Fever encounters",
};

export const signalLabel = (k: string) => SIGNAL_LABEL[k] ?? k.replaceAll("_", " ");
