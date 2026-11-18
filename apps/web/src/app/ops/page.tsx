"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import useSWR from "swr";
import { formatDistanceToNow } from "date-fns";
import { ArrowLeft, ExternalLink, Play, UserCheck, WifiOff, Zap } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { RoutingDiagram } from "@/components/ops/routing-diagram";
import { SEVERITY, ackAlert, type AlertItem, type Severity } from "@/lib/alerts";
import { fetcher } from "@/lib/api";
import {
  clientPlan, nodeStates, replayAlert, simulate, useAudit, useOpsAlerts, usePlan,
  type AuditEvent, type RoutingPayload,
} from "@/lib/ops";

/** Shown only if the API cannot be reached at all, so the demo story still completes. */
const OFFLINE_ALERT = {
  id: "offline-demo-alert", scenario_id: "heat-stress-cluster", scenario_label: "Heat-stress cluster", severity: "advisory",
  h3_cell: "offline", sites_affected: [], site_names: ["Al Maktoum International Airport expansion"], emirates: ["Dubai"],
  clinical_catchment: "Dubai", score: 0.53, credible_interval: [0.31, 0.74], coverage: 1, drivers: {}, drivers_prior: {},
  drivers_method: "shap", explanation_template: null, explanation_en: null, explanation_ar: null,
  explanation_source: "template_fallback", explanation_ar_source: null,
  action: { owner_role: "safety_officer", time_window: "within_1h", specific_action: "Move heavy outdoor tasks before 10:00 or after 16:00.",
    escalation_if_no_ack_in_minutes: 60, source: "template_fallback", model: null },
  driver_details: {}, created_at: "2026-06-16T10:00:00Z", peak_at: "2026-06-16T10:00:00Z", window_start: "2026-06-16T10:00:00Z",
  expires_at: "2026-06-16T10:00:00Z", as_of: "2026-06-16T10:00:00Z", action_owner: "site supervisor", recommended_actions: [],
} as unknown as AlertItem;

function shortReason(r?: string) {
  if (!r) return "n8n unreachable";
  if (/ConnectionError|ConnectTimeout|Max retries|NewConnectionError/.test(r)) return "n8n unreachable";
  if (/not configured/.test(r)) return "N8N_WEBHOOK_URL not set";
  return r.length > 60 ? `${r.slice(0, 57)}…` : r;
}

type Run = { t0: number; mode: "live" | "simulated"; reason?: string; payload: RoutingPayload; n8nHost?: string | null };

const KIND_LABEL: Record<string, string> = {
  received: "n8n received the alert", routed: "severity router", logged: "logged only (auto-ack)",
  notified_owner: "owner notified", ack_wait: "waiting for ack", acked: "acknowledged → done",
  escalated: "escalated: owner + escalation contact", escalation_wait: "waiting for ack",
  acked_after_escalation: "acknowledged after escalation", emirate_ops_lead: "final escalation: emirate operations lead",
};

function AuditRow({ e }: { e: AuditEvent }) {
  const t = new Date(e.ts);
  let what = "";
  if (e.kind === "send") what = `sent to n8n (${e.source}${e.rehearsal ? ", rehearsal" : ""})${e.ok === false ? ` · FAILED ${e.error ?? ""}` : ""}`;
  else if (e.kind === "ack") what = `acknowledged by ${e.by}${e.notes ? ` · ${e.notes}` : ""}`;
  else what = KIND_LABEL[e.step ?? ""] ?? e.step ?? "";
  return (
    <li className="flex gap-2 border-b py-1.5 text-xs last:border-0">
      <span className="w-16 shrink-0 tabular-nums text-muted-foreground">{t.toLocaleTimeString([], { hour12: false })}</span>
      <span className="min-w-0">
        <span className="font-medium">{e.kind}</span> · {what}
        {e.role && <span className="text-muted-foreground"> · {e.role.replaceAll("_", " ")}</span>}
        {e.channel && <span className="text-muted-foreground"> → {e.channel}</span>}
        {e.simulated && <Badge variant="outline" className="ml-1 px-1 py-0 text-[10px]">simulated</Badge>}
      </span>
    </li>
  );
}

export default function OpsPage() {
  const { data: alertsData, error: alertsError } = useOpsAlerts();
  const alerts = useMemo(() => {
    const a = alertsData?.alerts ?? [];
    return a.length ? a : alertsError || alertsData ? [OFFLINE_ALERT] : [];
  }, [alertsData, alertsError]);
  const [selected, setSelected] = useState<string | null>(null);
  const alert = alerts.find((a) => a.id === selected) ?? alerts[0] ?? null;
  const [asSeverity, setAsSeverity] = useState<Severity | null>(null);
  const { data: apiPlan } = usePlan(alert && alert.id !== OFFLINE_ALERT.id ? alert.id : null, asSeverity);
  const plan = apiPlan ?? (alert ? clientPlan(alert, asSeverity ?? alert.severity) : null);
  const [run, setRun] = useState<Run | null>(null);
  const [ackAt, setAckAt] = useState<number | null>(null);
  const [now, setNow] = useState(0);              // set by the ticker below (no clock reads in render)
  const { data: audit, mutate: refreshAudit } = useAudit(alert?.id ?? null, run != null);
  const { data: channels } = useSWR<{ groups: Record<string, { label: string; color: string }>; inbox: string }>("/api/ops/channels", fetcher);

  useEffect(() => {
    const tick = () => setNow(Date.now());
    const first = setTimeout(tick, 0);
    const t = setInterval(tick, run ? 500 : 5000);
    return () => { clearTimeout(first); clearInterval(t); };
  }, [run]);

  const realEvents = useMemo(() => audit?.events ?? [], [audit]);
  const liveEvents = useMemo(
    () => (run ? realEvents.filter((e) => e.kind === "route" && Date.parse(e.ts) >= run.t0 - 2000) : []),
    [run, realEvents]);
  // a live run that never reports back finishes the story with the simulation
  const stalled = !!run && run.mode === "live" && now - run.t0 > 8000 && liveEvents.length === 0;
  const effRun: Run | null = useMemo(
    () => (run && stalled ? { ...run, mode: "simulated", reason: "n8n did not report back within 8 s" } : run),
    [run, stalled]);
  const runEvents = useMemo(() => {
    if (!effRun) return [];
    if (effRun.mode === "simulated") {
      const humanAck = realEvents.find((e) => e.kind === "ack" && !e.auto && Date.parse(e.ts) >= effRun.t0);
      return simulate(effRun.payload, effRun.t0, now, ackAt ?? (humanAck ? Date.parse(humanAck.ts) : null));
    }
    return liveEvents;
  }, [effRun, realEvents, liveEvents, now, ackAt]);

  const states = plan && effRun ? nodeStates(effRun.payload, runEvents, now) : {};
  const shownPlan = effRun?.payload ?? plan;

  async function onReplay() {
    if (!alert || !plan) return;
    const t0 = Date.now();
    setAckAt(null);
    setNow(t0);
    if (alert.id === OFFLINE_ALERT.id) return setRun({ t0, mode: "simulated", reason: "API unreachable", payload: plan });
    try {
      const r = await replayAlert(alert.id, asSeverity);
      setRun(r.sent
        ? { t0, mode: "live", payload: r.payload, n8nHost: r.n8n_host }
        : { t0, mode: "simulated", reason: r.error ?? "webhook not reachable", payload: r.payload });
      refreshAudit();
    } catch (e) {
      setRun({ t0, mode: "simulated", reason: `API unreachable (${(e as Error).message})`, payload: plan });
    }
  }

  async function onAck() {
    if (!alert) return;
    setAckAt(Date.now());
    try {
      await ackAlert(alert.id, `${(shownPlan?.channels.owner.label ?? "Owner")} (ops demo)`, "acknowledged from /ops");
      refreshAudit();
    } catch {
      /* offline: the simulation still branches on ackAt */
    }
  }

  const auditShown = [...realEvents, ...(effRun?.mode === "simulated" ? runEvents : [])].sort((a, b) => Date.parse(b.ts) - Date.parse(a.ts));     // newest first

  return (
    <main className="min-h-screen bg-slate-50 dark:bg-slate-950">
      <header className="border-b border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
        <div className="mx-auto flex max-w-[1500px] items-center justify-between px-6 py-4">
          <div>
            <h1 className="text-xl font-semibold text-slate-900 dark:text-slate-100">Operations</h1>
            <p className="text-sm text-slate-500">Every alert routed to the responsible role through n8n, with escalation and an audit trail</p>
          </div>
          <Link href="/" className="inline-flex items-center gap-1 text-sm text-slate-600 hover:underline dark:text-slate-300">
            <ArrowLeft className="size-4" /> Live dashboard
          </Link>
        </div>
      </header>

      <div className="mx-auto grid max-w-[1500px] grid-cols-1 gap-4 px-6 py-6 lg:grid-cols-[280px_1fr_340px]">
        {/* left: compact feed */}
        <Card size="sm" className="lg:max-h-[calc(100vh-150px)] lg:overflow-y-auto">
          <CardHeader><CardTitle className="text-sm">Alert feed</CardTitle></CardHeader>
          <CardContent className="space-y-1.5">
            {!alertsData && !alertsError && <Skeleton className="h-40 w-full" />}
            {alertsError && (
              <div className="flex items-center gap-1 text-xs text-amber-700 dark:text-amber-400"><WifiOff className="size-3.5" /> API unreachable: offline demo alert</div>
            )}
            {!alertsError && alertsData && alertsData.alerts.length === 0 && (
              <div className="text-xs text-amber-700 dark:text-amber-400">
                No alerts in the current window (quiet season). Showing a demo fixture so the routing can be shown;
                its replay is simulated.
              </div>
            )}
            {alerts.map((a) => {
              const sev = SEVERITY[a.severity];
              const expired = Date.parse(a.expires_at) < now;
              return (
                <button key={a.id} type="button" onClick={() => { setSelected(a.id); setRun(null); setAsSeverity(null); }}
                  className={`w-full rounded-md border px-2 py-1.5 text-left text-xs hover:bg-muted/50 ${a.id === alert?.id ? "border-foreground/40 bg-muted/60" : ""}`}>
                  <div className="flex items-center gap-1.5">
                    <Badge className={`${sev.badge} px-1.5 py-0 text-[10px]`}>{sev.label}</Badge>
                    <span className="truncate font-medium">{a.scenario_label}</span>
                  </div>
                  <div className="truncate text-muted-foreground">{a.site_names.join(", ")}</div>
                  <div className="text-[10px] text-muted-foreground">
                    {expired ? "expired " : ""}{formatDistanceToNow(new Date(expired ? a.expires_at : a.created_at), { addSuffix: true })}
                  </div>
                </button>
              );
            })}
          </CardContent>
        </Card>

        {/* centre: routing */}
        <Card size="sm">
          <CardHeader>
            <CardTitle className="text-sm">Routing{alert ? ` · ${alert.scenario_label}, ${alert.site_names[0] ?? ""}` : ""}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {alert && shownPlan && (
              <>
                <div className="flex flex-wrap items-center gap-2 text-xs">
                  <Button size="sm" onClick={onReplay}><Play /> Replay alert through n8n</Button>
                  <span className="text-muted-foreground">rehearse as</span>
                  <div role="group" aria-label="Rehearsal severity" className="inline-flex rounded-lg border p-0.5">
                    {([null, "watch", "advisory", "warning"] as (Severity | null)[]).map((s) => (
                      <Button key={s ?? "asis"} size="xs" variant={asSeverity === s ? "default" : "ghost"} aria-pressed={asSeverity === s}
                        onClick={() => { setAsSeverity(s); setRun(null); }}>
                        {s ? SEVERITY[s].label : `as is (${SEVERITY[alert.severity].label})`}
                      </Button>
                    ))}
                  </div>
                  <Button size="sm" variant="outline" onClick={onAck} disabled={!run}><UserCheck /> Acknowledge as owner</Button>
                </div>
                <div className="flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
                  {effRun?.mode === "live" && <Badge variant="outline" className="gap-1"><Zap className="size-3" /> live: n8n at {effRun.n8nHost}</Badge>}
                  {effRun?.mode === "simulated" && (
                    <Badge variant="outline" className="gap-1 border-amber-500/50" title={effRun.reason}>
                      <WifiOff className="size-3" /> simulated: {shortReason(effRun.reason)}
                    </Badge>
                  )}
                  {(run?.payload.rehearsal || asSeverity) && <Badge variant="outline">rehearsal: true severity is {SEVERITY[alert.severity].label}</Badge>}
                  <span>Owner: <span className="font-medium text-foreground">{shownPlan.channels.owner.label}</span></span>
                  <span>· escalation contact: <span className="font-medium text-foreground">{shownPlan.channels.supervisor.label}</span></span>
                  <span>· waits {shownPlan.waits.ack_wait_s}s (demo)</span>
                </div>
                <RoutingDiagram plan={shownPlan} states={effRun ? states : {}} />
                <p className="rounded-md bg-muted/50 px-2.5 py-2 text-xs">
                  <span className="font-medium">Action for {shownPlan.channels.owner.label}:</span> {shownPlan.action.specific_action}
                </p>
                {channels && (
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
                    {Object.entries(channels.groups).map(([k, g]) => (
                      <span key={k} className="flex items-center gap-1"><span className="size-2.5 rounded-sm" style={{ background: g.color }} />{g.label}</span>
                    ))}
                    <a href={channels.inbox} target="_blank" rel="noreferrer" className="ml-auto inline-flex items-center gap-1 underline underline-offset-2">
                      demo channel inbox <ExternalLink className="size-3" />
                    </a>
                  </div>
                )}
              </>
            )}
          </CardContent>
        </Card>

        {/* right: audit */}
        <Card size="sm" className="lg:max-h-[calc(100vh-150px)] lg:overflow-y-auto">
          <CardHeader><CardTitle className="text-sm">Audit log <span className="font-normal text-muted-foreground">· newest first</span></CardTitle></CardHeader>
          <CardContent>
            {auditShown.length ? (
              <ul>{auditShown.map((e, i) => <AuditRow key={`${e.ts}-${e.kind}-${e.step ?? ""}-${i}`} e={e} />)}</ul>
            ) : (
              <div className="text-xs text-muted-foreground">No routing or acknowledgements recorded for this alert yet.</div>
            )}
          </CardContent>
        </Card>
      </div>
    </main>
  );
}
