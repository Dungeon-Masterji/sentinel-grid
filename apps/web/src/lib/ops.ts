import useSWR from "swr";
import { API_BASE, fetcher } from "@/lib/api";
import type { AlertItem, Severity } from "@/lib/alerts";

export type Channel = {
  role: string;
  label: string;
  group: string;
  group_label: string;
  color: string;
  url: string;
  url_source: "env" | "demo";
};

export type PlanStep = {
  id: StepId;
  label: string;
  role?: string;
  color?: string;
  wait_s?: number;
  terminal?: boolean;
  branch?: "ok" | "escalate" | "timeout";
};

export type StepId =
  | "received" | "routed" | "logged" | "notified_owner" | "ack_wait" | "acked"
  | "escalated" | "escalation_wait" | "acked_after_escalation" | "emirate_ops_lead";

export type RoutingPayload = {
  alert_id: string;
  scenario_label: string;
  severity: Severity;
  location_name: string;
  action: { owner_role: string; specific_action: string; time_window: string; escalation_if_no_ack_in_minutes: number };
  channels: { owner: Channel; supervisor: Channel; emirate_ops_lead: Channel };
  waits: { ack_wait_s: number; escalation_wait_s: number };
  plan: PlanStep[];
  demo: boolean;
  rehearsal?: boolean;
  true_severity?: Severity;
};

export type AuditEvent = {
  ts: string;
  kind: "send" | "ack" | "route";
  alert_id: string;
  step?: StepId;
  role?: string | null;
  channel?: string | null;
  by?: string;
  notes?: string;
  auto?: boolean;
  source?: string;
  ok?: boolean;
  error?: string | null;
  rehearsal?: boolean;
  simulated?: boolean;
};

export type ReplayResponse = {
  sent: boolean;
  http_status: number | null;
  error: string | null;
  n8n_host: string | null;
  simulate: boolean;
  payload: RoutingPayload;
};

export const useOpsAlerts = () =>
  useSWR<{ alerts: AlertItem[] }>("/api/alerts?include_expired=true", fetcher, { refreshInterval: 60_000 });

export const usePlan = (id: string | null, asSeverity: Severity | null) =>
  useSWR<RoutingPayload>(id ? `/api/ops/plan/${id}?demo=true${asSeverity ? `&as_severity=${asSeverity}` : ""}` : null, fetcher);

export const useAudit = (id: string | null, live: boolean) =>
  useSWR<{ events: AuditEvent[] }>(id ? `/api/ops/audit?alert_id=${id}` : null, fetcher, {
    refreshInterval: live ? 1500 : 15_000,
  });

export async function replayAlert(id: string, asSeverity: Severity | null): Promise<ReplayResponse> {
  const q = new URLSearchParams({ demo: "true" });
  if (asSeverity) q.set("as_severity", asSeverity);
  const r = await fetch(`${API_BASE}/api/ops/replay_alert/${id}?${q}`, { method: "POST" });
  if (!r.ok) throw new Error(`replay failed (${r.status})`);
  return r.json();
}

/** Mirror of notify.routing_plan, for when the API itself is unreachable. */
export function clientPlan(alert: AlertItem, severity: Severity): RoutingPayload {
  const role = alert.action?.owner_role ?? "emirate_ops_lead";
  const ch = (r: string, label: string, color: string): Channel =>
    ({ role: r, label, group: "", group_label: "", color, url: "", url_source: "demo" });
  const owner = ch(role, role.replaceAll("_", " "), "#2a78d6");
  const supervisor = ch("escalation_contact", "escalation contact", "#6250d6");
  const lead = ch("emirate_ops_lead", "Emirate operations lead", "#e87ba4");
  const w = 10;
  const steps: PlanStep[] = [{ id: "received", label: "n8n webhook received" }, { id: "routed", label: `Severity router → ${severity}` }];
  if (severity === "watch") steps.push({ id: "logged", label: "Logged only (auto-ack)", terminal: true });
  if (severity === "advisory")
    steps.push(
      { id: "notified_owner", label: `Notify ${owner.label}`, role, color: owner.color },
      { id: "ack_wait", label: `Wait ${w} s for ack`, wait_s: w },
      { id: "acked", label: "Acknowledged → done", terminal: true, branch: "ok" },
    );
  if (severity !== "watch")
    steps.push(
      { id: "escalated", label: `Notify ${owner.label} + escalation contact`, color: supervisor.color, branch: "escalate" },
      { id: "escalation_wait", label: `Wait ${w} s for ack`, wait_s: w },
      { id: "acked_after_escalation", label: "Acknowledged → done", terminal: true, branch: "ok" },
      { id: "emirate_ops_lead", label: "Escalate to Emirate operations lead", color: lead.color, terminal: true, branch: "timeout" },
    );
  return {
    alert_id: alert.id, scenario_label: alert.scenario_label, severity, location_name: alert.site_names.join(", "),
    action: {
      owner_role: role, specific_action: alert.action?.specific_action ?? alert.recommended_actions[0] ?? "",
      time_window: alert.action?.time_window ?? "", escalation_if_no_ack_in_minutes: alert.action?.escalation_if_no_ack_in_minutes ?? 60,
    },
    channels: { owner, supervisor, emirate_ops_lead: lead }, waits: { ack_wait_s: w, escalation_wait_s: w },
    plan: steps, demo: true,
  };
}

/** Deterministic stand-in for n8n: the events the workflow WOULD have posted by `now`,
 *  given the plan, the start time and (if any) when a human acknowledged. */
export function simulate(p: RoutingPayload, t0: number, now: number, ackAt: number | null): AuditEvent[] {
  const out: AuditEvent[] = [];
  const ev = (t: number, step: StepId, role?: string) => {
    if (t <= now) out.push({ ts: new Date(t).toISOString(), kind: "route", alert_id: p.alert_id, step, role, simulated: true });
  };
  let t = t0 + 400;
  ev(t, "received");
  t += 600;
  ev(t, "routed");
  t += 800;
  if (p.severity === "watch") {
    ev(t, "logged");
    return out;
  }
  const acked = (until: number) => ackAt != null && ackAt <= until;
  if (p.severity === "advisory") {
    ev(t, "notified_owner", p.channels.owner.role);
    const end = t + p.waits.ack_wait_s * 1000;
    if (acked(end)) {
      ev(Math.max(ackAt as number, t) + 600, "acked");
      return out;
    }
    t = end + 600;
  }
  ev(t, "escalated", p.channels.supervisor.role);
  const end = t + p.waits.escalation_wait_s * 1000;
  if (acked(end)) ev(Math.max(ackAt as number, t) + 600, "acked_after_escalation");
  else ev(end + 600, "emirate_ops_lead", p.channels.emirate_ops_lead.role);
  return out;
}

export type NodeState = "pending" | "active" | "done" | "acked" | "escalated" | "timeout" | "skipped";

/** Diagram state of every plan step from the route events of one run. */
export function nodeStates(p: RoutingPayload, events: AuditEvent[], now: number): Record<string, { state: NodeState; at?: number; left_s?: number }> {
  const at: Partial<Record<StepId, number>> = {};
  for (const e of events) if (e.kind === "route" && e.step && at[e.step] == null) at[e.step] = Date.parse(e.ts);
  const s: Record<string, { state: NodeState; at?: number; left_s?: number }> = {};
  const done = (id: StepId, st: NodeState = "done") => (s[id] = { state: st, at: at[id] });
  const ids = p.plan.map((x) => x.id);
  for (const id of ids) s[id] = { state: "pending" };
  for (const id of ["received", "routed", "logged", "notified_owner"] as StepId[]) if (at[id] != null) done(id);
  if (at.escalated != null) done("escalated", "escalated");
  if (at.acked != null) done("acked", "acked");
  if (at.acked_after_escalation != null) done("acked_after_escalation", "acked");
  if (at.emirate_ops_lead != null) done("emirate_ops_lead", "timeout");
  const waitState = (w: StepId, from: StepId, ends: StepId[], secs: number) => {
    if (!ids.includes(w) || at[from] == null) return;
    const end = ends.map((e) => at[e]).find((x) => x != null);
    if (end != null) s[w] = { state: "done", at: end };
    else s[w] = { state: "active", left_s: Math.max(0, Math.ceil(secs - (now - (at[from] as number)) / 1000)) };
  };
  waitState("ack_wait", "notified_owner", ["acked", "escalated"], p.waits.ack_wait_s);
  waitState("escalation_wait", "escalated", ["acked_after_escalation", "emirate_ops_lead"], p.waits.escalation_wait_s);
  if (at.acked != null) for (const id of ["escalated", "escalation_wait", "acked_after_escalation", "emirate_ops_lead"]) if (s[id]) s[id] = { state: "skipped" };
  if (at.escalated != null && s.acked) s.acked = { state: "skipped" };
  if (at.acked_after_escalation != null && s.emirate_ops_lead) s.emirate_ops_lead = { state: "skipped" };
  if (at.emirate_ops_lead != null && s.acked_after_escalation) s.acked_after_escalation = { state: "skipped" };
  // the first pending step right after a finished one is "active"
  for (let i = 1; i < ids.length; i++) {
    const prev = s[ids[i - 1]].state;
    if (s[ids[i]].state === "pending" && ["done", "escalated"].includes(prev) && p.plan[i].branch !== "ok" && p.plan[i].branch !== "timeout") {
      s[ids[i]] = { ...s[ids[i]], state: "active" };
      break;
    }
  }
  return s;
}
