"use client";

import { useState } from "react";
import { CheckCircle2, CircleAlert, Clock, Loader2, UserRound } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { T, type Lang } from "@/components/alerts/shap-why";
import { OWNER_ROLE, TIME_WINDOW, ackAlert, type AlertItem } from "@/lib/alerts";

/** "Recommended action" card: the structured action from the LLM (or its template fallback). */
export function ActionCard({ alert, lang }: { alert: AlertItem; lang: Lang }) {
  const a = alert.action;
  if (!a) return null;
  const t = T[lang];
  return (
    <Card size="sm" className="gap-2">
      <CardHeader>
        <CardTitle className="flex items-center gap-1.5 text-sm">
          <CircleAlert className="size-4 text-orange-500" aria-hidden />
          {t.action}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        {/* the action sentence is English; mark it so screen readers and bidi get it right */}
        <p lang="en" dir="ltr" className={`text-sm font-medium ${lang === "ar" ? "text-right" : ""}`}>
          {a.specific_action}
        </p>
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
          <span className="flex items-center gap-1">
            <Clock className="size-3.5" aria-hidden />
            {t.window}: <span className="font-medium text-foreground">{TIME_WINDOW[a.time_window]?.[lang] ?? a.time_window}</span>
          </span>
          <span className="flex items-center gap-1">
            <UserRound className="size-3.5" aria-hidden />
            {t.owner}: <span className="font-medium text-foreground">{OWNER_ROLE[a.owner_role]?.[lang] ?? a.owner_role}</span>
          </span>
        </div>
        <div className="text-[11px] text-muted-foreground">
          {t.escalate(a.escalation_if_no_ack_in_minutes)}
          {a.source !== "llm" && ` · ${t.template}`}
        </div>
      </CardContent>
    </Card>
  );
}

/** One-click acknowledge (POST /api/alerts/{id}/ack) as the action's owner role. */
export function AckButton({ alert, lang, onDone }: { alert: AlertItem; lang: Lang; onDone: () => void }) {
  const t = T[lang];
  const defaultRole = alert.action ? OWNER_ROLE[alert.action.owner_role]?.en ?? alert.action.owner_role : alert.action_owner;
  const [by, setBy] = useState(defaultRole);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  return (
    <form
      className="space-y-1"
      onSubmit={async (e) => {
        e.preventDefault();
        if (!by.trim()) return;
        setBusy(true);
        setErr(null);
        try {
          await ackAlert(alert.id, by.trim(), "");
          setDone(true);
          onDone();
        } catch (x) {
          setErr(String((x as Error).message));
        } finally {
          setBusy(false);
        }
      }}
    >
      <div className="flex items-center gap-2">
        <input
          value={by}
          onChange={(e) => setBy(e.target.value)}
          aria-label={t.yourRole}
          maxLength={120}
          className="min-w-0 flex-1 rounded-md border border-slate-200 bg-transparent px-2.5 py-1 text-sm dark:border-slate-700"
        />
        <Button type="submit" size="sm" disabled={busy || !by.trim()}>
          {busy ? <Loader2 className="animate-spin" /> : <CheckCircle2 />}
          {done ? t.acked : t.ack}
        </Button>
      </div>
      {err && <div className="text-xs text-rose-600">{err}</div>}
    </form>
  );
}
