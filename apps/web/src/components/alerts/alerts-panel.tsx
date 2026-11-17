"use client";

import { useMemo, useState } from "react";
import { formatDistanceToNow } from "date-fns";
import { MapPin, UserRound } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { AlertSheet } from "@/components/alerts/alert-sheet";
import { DriverBar } from "@/components/alerts/driver-bar";
import { SEVERITY, useAlerts, type AlertItem, type Severity } from "@/lib/alerts";

const selectCls =
  "h-8 rounded-md border border-slate-200 bg-white px-2 text-xs text-slate-700 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300";

function AlertCard({ a, onOpen }: { a: AlertItem; onOpen: () => void }) {
  const sev = SEVERITY[a.severity];
  return (
    <button type="button" onClick={onOpen} className="block w-full text-left">
      <Card className="relative transition-shadow hover:ring-foreground/25">
        <span className={`absolute inset-y-0 left-0 w-1 ${sev.rail}`} aria-hidden />
        <CardContent className="space-y-2.5 pl-5">
          <div className="flex flex-wrap items-center gap-2">
            <Badge className={sev.badge}>{sev.label}</Badge>
            <span className="text-sm font-medium">{a.scenario_label}</span>
            <span className="ml-auto text-xs text-slate-500">
              {formatDistanceToNow(new Date(a.created_at), { addSuffix: true })}
            </span>
          </div>
          <div className="flex items-start gap-1.5 text-xs text-slate-600 dark:text-slate-400">
            <MapPin className="mt-0.5 h-3 w-3 shrink-0" />
            <span>
              {a.site_names.join(", ")} · {a.emirates.join(", ")}
            </span>
          </div>
          <DriverBar drivers={a.drivers} details={a.driver_details} />
          <div className="text-sm">{a.recommended_actions[0]}</div>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
            <span className="flex items-center gap-1">
              <UserRound className="h-3 w-3" />
              {a.action_owner}
            </span>
            <span title={a.drivers_method === "shap" ? "Driver shares from SHAP on a surrogate model" : "Driver shares from fusion weights (no surrogate for this hex)"}>
              {a.drivers_method === "shap" ? "SHAP drivers" : "weight drivers"}
            </span>
            <span className="tabular-nums">
              score {a.score.toFixed(2)} [{a.credible_interval[0].toFixed(2)}–{a.credible_interval[1].toFixed(2)}]
            </span>
          </div>
        </CardContent>
      </Card>
    </button>
  );
}

export function AlertsPanel() {
  const { data, error, isLoading } = useAlerts();
  const [severity, setSeverity] = useState<"" | Severity>("");
  const [scenario, setScenario] = useState("");
  const [mySites, setMySites] = useState(false);
  const [open, setOpen] = useState<AlertItem | null>(null);

  const scenarios = useMemo(() => {
    const m = new Map<string, string>();
    data?.alerts.forEach((a) => m.set(a.scenario_id, a.scenario_label));
    return [...m.entries()];
  }, [data]);

  const shown = (data?.alerts ?? []).filter(
    (a) =>
      (!severity || SEVERITY[a.severity].rank >= SEVERITY[severity].rank) &&
      (!scenario || a.scenario_id === scenario)
    // "my sites only" is stubbed until auth lands: no user -> site assignment yet.
  );

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <select aria-label="Minimum severity" className={selectCls} value={severity} onChange={(e) => setSeverity(e.target.value as "" | Severity)}>
          <option value="">All severities</option>
          <option value="watch">Watch +</option>
          <option value="advisory">Advisory +</option>
          <option value="warning">Warning</option>
        </select>
        <select aria-label="Scenario" className={selectCls} value={scenario} onChange={(e) => setScenario(e.target.value)}>
          <option value="">All scenarios</option>
          {scenarios.map(([id, label]) => (
            <option key={id} value={id}>
              {label}
            </option>
          ))}
        </select>
        <label className="flex cursor-pointer items-center gap-2 text-xs text-slate-600 dark:text-slate-400" title="Needs sign-in: site assignments are not wired yet">
          <button
            type="button"
            role="switch"
            aria-checked={mySites}
            onClick={() => setMySites((v) => !v)}
            className={`relative h-4 w-7 rounded-full transition-colors ${mySites ? "bg-slate-900 dark:bg-slate-100" : "bg-slate-300 dark:bg-slate-700"}`}
          >
            <span className={`absolute top-0.5 h-3 w-3 rounded-full bg-white transition-all dark:bg-slate-900 ${mySites ? "left-3.5" : "left-0.5"}`} />
          </button>
          My sites only
          {mySites && <span className="text-[11px] text-slate-400">(coming with sign-in)</span>}
        </label>
        {data?.as_of && (
          <span className="ml-auto text-xs text-slate-500">
            Detector run {formatDistanceToNow(new Date(data.as_of), { addSuffix: true })}
          </span>
        )}
      </div>

      {isLoading && (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          <Skeleton className="h-40 rounded-xl" />
          <Skeleton className="h-40 rounded-xl" />
        </div>
      )}
      {error && !data && <div className="text-sm text-rose-600">Alert feed unreachable</div>}
      {data && shown.length === 0 && (
        <div className="text-sm text-slate-500">
          {data.count === 0 ? "No active alerts. All scenarios are below their watch thresholds." : "No alerts match these filters."}
        </div>
      )}
      {shown.length > 0 && (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          {shown.map((a) => (
            <AlertCard key={a.id} a={a} onOpen={() => setOpen(a)} />
          ))}
        </div>
      )}
      <AlertSheet alert={open} onOpenChange={(o) => !o && setOpen(null)} />
    </div>
  );
}
