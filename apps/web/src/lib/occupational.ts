import useSWR from "swr";
import { fetcher } from "@/lib/api";

export type RiskTier = "green" | "amber" | "red";

export type SiteStatus = {
  site_id: string;
  name: string;
  operator: string;
  sector: "construction" | "logistics" | "industrial" | "ports" | "aviation";
  emirate: string;
  lat: number;
  lon: number;
  h3_res8: string;
  shift_pattern: string;
  heat_exposure: string;
  worker_count: number;
  scheduled_today: number | null;
  worked_today: number | null;
  absence_rate: number | null;
  heat_absence_rate: number | null;
  respiratory_absence_rate: number | null;
  mean_ppe_compliance: number | null;
  mean_apparent_temp_today: number | null;
  max_apparent_temp_today: number | null;
  mean_pm25_today: number | null;
  workers_on_site_now: number | null;
  shift_now: string | null;
  apparent_temp_now: number | null;
  observed_at: string | null;
  risk_tier: RiskTier;
  risk_score: number;
  worked_hours_7d: { date: string; worked_hours: number | null }[];
};

export type SitesResponse = {
  synthetic: boolean;
  notice: string;
  generated_at: string | null;
  as_of: string;
  current_day: string;
  totals: {
    sites: number;
    roster: number;
    active_today: number;
    on_site_now: number;
    elevated_sites: number;
    watch_sites: number;
  };
  risk_definition: Record<string, string>;
  sites: SiteStatus[];
};

/** Shared by the pillar card, site grid and map: SWR dedupes to one request. */
export function useOccupationalSites() {
  return useSWR<SitesResponse>("/api/signals/occupational/sites", fetcher, {
    refreshInterval: 300_000,
  });
}

export const TIER: Record<RiskTier, { label: string; dot: string; hex: string; badge: string }> = {
  green: {
    label: "Normal",
    dot: "bg-emerald-500",
    hex: "#10b981",
    badge: "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300",
  },
  amber: {
    label: "Watch",
    dot: "bg-amber-500",
    hex: "#f59e0b",
    badge: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300",
  },
  red: {
    label: "Elevated",
    dot: "bg-rose-500",
    hex: "#f43f5e",
    badge: "bg-rose-100 text-rose-800 dark:bg-rose-950 dark:text-rose-300",
  },
};

export const pct = (x: number | null | undefined, digits = 1) =>
  x == null ? "—" : `${(x * 100).toFixed(digits)}%`;
