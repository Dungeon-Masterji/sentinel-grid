import useSWR from "swr";
import { fetcher } from "@/lib/api";

export type Overall = {
  events: number;
  detected: number;
  sensitivity: number | null;
  mean_lead_time_hours: number | null;
  median_lead_time_hours: number | null;
  detected_before_start: number;
  alerts: number;
  matched_alerts: number;
  false_alarms: number;
  ppv: number | null;
  false_alarms_per_week: number | null;
  brier: number | null;
  brier_climatology: number | null;
  brier_skill: number | null;
};

export type Chance = {
  n: number;
  mode: "uniform" | "seasonal";
  sensitivity_mean: number | null;
  sensitivity_p95: number | null;
  ppv_mean: number | null;
  ppv_p95: number | null;
};

export type TierBlock = Overall & {
  chance_uniform?: Chance;
  chance_seasonal?: Chance;
  sensitivity_lift?: number | null;
  ppv_lift?: number | null;
};

export type ScenarioBlock = Partial<Overall> & { alerts: number; ppv: number | null; false_alarms_per_week: number | null };

export type CalibrationBin = {
  bin: number;
  lo: number;
  hi: number;
  n: number;
  mean_predicted: number | null;
  observed_frequency: number | null;
};

export type Calibration = {
  bins: CalibrationBin[];
  brier: number | null;
  brier_climatology: number | null;
  brier_skill: number | null;
  n: number;
  positives: number;
  base_rate?: number;
};

export type Scorecard = {
  notice: string;
  window: { start?: string; end?: string; weeks: number };
  overall: Overall & { chance_seasonal?: Chance; sensitivity_lift?: number | null; ppv_lift?: number | null };
  by_tier: Record<"watch" | "advisory" | "warning", TierBlock>;
  per_scenario: Record<string, ScenarioBlock>;
  by_event_type: Record<string, Partial<Overall>>;
  coverage_matrix: {
    scenarios: string[];
    event_types: string[];
    values: (number | null)[][];
    n_events: Record<string, number>;
  };
  calibration: Calibration | null;
  generated_at: string;
  timings?: Record<string, number | null>;
};

export type BacktestEvent = {
  id: string;
  label: string;
  event_type: string;
  start: string;
  end: string;
  peak: string;
  affected_emirates: string[];
  n_hexes: number;
  expected_scenarios: string[];
  severity_truth: string;
  peak_value: string | null;
  source_url: string;
  notes: string;
  detected: boolean;
  lead_time_hours: number | null;
  first_alert_time: string | null;
  alert_severity_at_first_hit: string | null;
  peak_severity_during_event: string | null;
  n_matching_alerts: number;
  n_alerts_any_scenario?: number;
};

export type ReplayAlert = {
  id: string;
  scenario_id: string;
  h3_cell: string;
  first_seen: string;
  severity_first: string;
  severity_max: string;
  score_max: number;
  expected: boolean;
};

export type ReplayHour = { ts: string; signals: Record<string, number | null>; fused: Record<string, number | null> };

export type ReplayResponse = {
  event: BacktestEvent & { alerts: ReplayAlert[] };
  hours: ReplayHour[];
};

export const useScorecard = () => useSWR<Scorecard>("/api/backtest/scorecard", fetcher);
export const useBacktestEvents = () => useSWR<{ events: BacktestEvent[] }>("/api/backtest/events", fetcher);
export const useCalibration = () =>
  useSWR<{ overall: Calibration; per_scenario: Record<string, Calibration> }>("/api/backtest/calibration", fetcher);
export const useReplay = (id: string | null) =>
  useSWR<ReplayResponse>(id ? `/api/backtest/replay/${id}` : null, fetcher);

/** Fixed entity -> categorical slot (never by rank). */
export const SCENARIO: Record<string, { label: string; color: string }> = {
  "heat-stress-cluster": { label: "Heat-stress", color: "var(--bt-scn-1)" },
  "respiratory-dust-event": { label: "Respiratory / dust", color: "var(--bt-scn-2)" },
  "infectious-cluster-signal": { label: "Infectious", color: "var(--bt-scn-3)" },
};

export const EVENT_TYPE: Record<string, string> = {
  heatwave: "Heatwave",
  dust_storm: "Dust storm",
  flood: "Flood",
  ramadan_stress: "Ramadan stress",
  infectious_cluster: "Infectious cluster",
  pollution_episode: "Pollution episode",
  heat_wave_combined_with_dust: "Heat + dust",
};
