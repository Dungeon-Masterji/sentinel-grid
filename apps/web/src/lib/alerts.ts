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
  label?: string | null;
};

export type ShapSignal = {
  abs_shap: number;
  shap: number;
  pct_of_total: number;
  direction: "raises_risk" | "lowers_risk";
};

export type ShapSummary = {
  signals: Record<string, ShapSignal>;
  base_value: number;
  prediction: number;
  model: { model_fitted_at?: string; n_train?: number; r2_holdout?: number; reused?: boolean } | null;
  background_rows: number | null;
};

export type WaterfallStep = {
  feature: string;
  signal: string;
  pillar: Pillar | null;
  shap: number;
  value: number | null;
  baseline: number;
  cumulative: number;
};

export type WaterfallResponse = {
  alert_id: string;
  task: "regression" | "classification";
  level: "signal" | "feature";
  output: string;
  base_value: number;
  prediction: number;
  steps: WaterfallStep[];
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
  /** signal -> contribution %, sums to 100: SHAP shares (drivers_method "shap"), or the
   *  fusion-weight shares when no surrogate could be fitted ("fusion_weights"). */
  drivers: Record<string, number>;
  /** static fusion-weight shares (%), kept for comparison */
  drivers_prior: Record<string, number>;
  drivers_method: "shap" | "fusion_weights";
  explanation_template: string | null;
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
  shap: ShapSummary | null;
  driver_onsets: Record<string, string | null>;
  shap_disagreements: { signal: string; fusion_weight: number; shap: number; pct_of_total: number }[];
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

export function useShapWaterfall(id: string | null, enabled = true) {
  return useSWR<WaterfallResponse>(id && enabled ? `/api/alerts/${id}/shap_waterfall` : null, fetcher);
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

/**
 * Pillar hues match the pillar card icons (amber env, teal clinical, rose occupational).
 * One hue per pillar: validated with the dataviz palette checker in light AND dark mode
 * (lightness band, chroma, CVD ΔE >= 9.3 all-pairs, normal ΔE >= 16.6, contrast >= 3:1).
 * Lighter/darker shades of the same hue failed those checks, so a second signal from the
 * same pillar is told apart by a diagonal hatch (see hatchFill) + its direct label.
 */
export const PILLAR: Record<Pillar, { label: string; hex: string }> = {
  env: { label: "Environmental", hex: "#d97706" },
  occ: { label: "Occupational", hex: "#e11d48" },
  clin: { label: "Clinical", hex: "#0d9488" },
};

/** CSS background for a segment; nth >= 1 within a pillar gets the 45° hatch. */
export function pillarFill(pillar: Pillar, nth: number): string {
  const c = PILLAR[pillar].hex;
  return nth === 0
    ? c
    : `repeating-linear-gradient(45deg, ${c} 0 3px, color-mix(in srgb, ${c} 45%, transparent) 3px 6px)`;
}

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
