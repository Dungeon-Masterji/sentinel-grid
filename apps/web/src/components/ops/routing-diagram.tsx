"use client";

import { AlarmClock, ArrowUpRight, CheckCircle2, ChevronRight, Circle, Hourglass, Loader2, MinusCircle, UserCheck } from "lucide-react";
import type { NodeState, PlanStep, RoutingPayload, StepId } from "@/lib/ops";

const LANES: { sev: "watch" | "advisory" | "warning"; label: string; steps: StepId[] }[] = [
  { sev: "watch", label: "Watch · log only", steps: ["logged"] },
  { sev: "advisory", label: "Advisory · notify owner, wait for ack", steps: ["notified_owner", "ack_wait", "acked"] },
  { sev: "warning", label: "Warning · owner + escalation contact", steps: ["escalated", "escalation_wait", "acked_after_escalation", "emirate_ops_lead"] },
];

/** State is carried by icon + label, never colour alone; role colour is the left stripe. */
const STATE: Record<NodeState, { label: string; icon: typeof Circle; cls: string }> = {
  pending: { label: "pending", icon: Circle, cls: "text-muted-foreground" },
  active: { label: "in progress", icon: Loader2, cls: "text-foreground" },
  done: { label: "sent", icon: CheckCircle2, cls: "text-emerald-700 dark:text-emerald-400" },
  acked: { label: "acknowledged", icon: UserCheck, cls: "text-emerald-700 dark:text-emerald-400" },
  escalated: { label: "escalated", icon: ArrowUpRight, cls: "text-amber-700 dark:text-amber-400" },
  timeout: { label: "no ack · final escalation", icon: AlarmClock, cls: "text-rose-700 dark:text-rose-400" },
  skipped: { label: "not needed", icon: MinusCircle, cls: "text-muted-foreground" },
};

function Node({ step, st }: { step: PlanStep | { id: string; label: string; color?: string; wait_s?: number }; st: { state: NodeState; left_s?: number } }) {
  const S = STATE[st.state];
  const Icon = step.wait_s && st.state === "active" ? Hourglass : S.icon;
  const dim = st.state === "pending" || st.state === "skipped";
  return (
    <div
      className={`relative min-w-0 flex-1 rounded-lg border bg-card px-2.5 py-2 transition-all ${dim ? "opacity-55" : "shadow-sm"} ${st.state === "active" ? "ring-2 ring-foreground/30" : ""}`}
      style={{ borderLeft: `4px solid ${step.color ?? "var(--border)"}` }}
    >
      <div className="text-xs font-medium leading-snug">{step.label}</div>
      <div className={`mt-1 flex items-center gap-1 text-[11px] ${S.cls}`}>
        <Icon className={`size-3.5 ${st.state === "active" && !step.wait_s ? "animate-spin" : ""}`} aria-hidden />
        {step.wait_s && st.state === "active" ? `waiting · ${st.left_s ?? step.wait_s}s left` : S.label}
      </div>
    </div>
  );
}

const Arrow = () => <ChevronRight className="size-4 shrink-0 text-muted-foreground" aria-hidden />;

export function RoutingDiagram({ plan, states }: { plan: RoutingPayload; states: Record<string, { state: NodeState; left_s?: number }> }) {
  const byId = new Map(plan.plan.map((s) => [s.id, s]));
  const st = (id: string) => states[id] ?? { state: "pending" as NodeState };
  return (
    <div className="space-y-3" role="group" aria-label="Alert routing diagram">
      <div className="flex items-center gap-1.5">
        <Node step={byId.get("received") ?? { id: "received", label: "n8n webhook received" }} st={st("received")} />
        <Arrow />
        <Node step={byId.get("routed") ?? { id: "routed", label: "Severity router" }} st={st("routed")} />
      </div>
      {LANES.map((lane) => {
        const inPlan = lane.steps.some((id) => byId.has(id));
        const viaEscalation = lane.sev === "warning" && plan.severity === "advisory";
        return (
          <div key={lane.sev} className={`rounded-xl border border-dashed p-2.5 ${inPlan ? "" : "opacity-35"}`}>
            <div className="mb-1.5 flex items-center justify-between text-[11px] uppercase tracking-wider text-muted-foreground">
              <span>{lane.label}</span>
              <span className="normal-case tracking-normal">
                {!inPlan ? "not on this alert's path" : viaEscalation ? "only if the owner does not acknowledge" : plan.severity === lane.sev ? "this alert" : ""}
              </span>
            </div>
            <div className="flex items-stretch gap-1.5">
              {lane.steps.map((id, i) => {
                const step = byId.get(id) ?? { id, label: id.replaceAll("_", " ") };
                return (
                  <div key={id} className="flex min-w-0 flex-1 items-center gap-1.5">
                    {i > 0 && <Arrow />}
                    <Node step={step} st={inPlan ? st(id) : { state: "pending" }} />
                  </div>
                );
              })}
            </div>
          </div>
        );
      })}
    </div>
  );
}
