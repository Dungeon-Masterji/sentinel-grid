"use client";

import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { ScorecardTiles } from "@/components/backtest/scorecard-tiles";
import { CoverageHeatmap } from "@/components/backtest/coverage-heatmap";
import { CalibrationPlot } from "@/components/backtest/calibration-plot";
import { EventList } from "@/components/backtest/event-list";
import { TierTable } from "@/components/backtest/tier-table";
import { SCENARIO, useBacktestEvents, useCalibration, useScorecard } from "@/lib/backtest";

export default function BacktestPage() {
  const { data: card, error } = useScorecard();
  const { data: ev } = useBacktestEvents();
  const { data: cal } = useCalibration();

  return (
    <main className="min-h-screen bg-slate-50 dark:bg-slate-950">
      <header className="border-b border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-6 py-4">
          <div>
            <h1 className="text-xl font-semibold text-slate-900 dark:text-slate-100">Backtest</h1>
            <p className="text-sm text-slate-500">
              The live detector replayed over 2023–2024 UAE events, every 15 minutes, with zero lookahead
            </p>
          </div>
          <Link href="/" className="inline-flex items-center gap-1 text-sm text-slate-600 hover:underline dark:text-slate-300">
            <ArrowLeft className="size-4" /> Live dashboard
          </Link>
        </div>
      </header>

      <div className="mx-auto max-w-7xl space-y-6 px-6 py-8">
        {error && <div className="text-sm text-rose-600">Backtest results unavailable ({String(error.message)}).</div>}
        {card ? (
          <>
            <p className="text-xs text-muted-foreground">
              {card.notice} Window {card.window.start?.slice(0, 10)} → {card.window.end?.slice(0, 10)} · run{" "}
              {card.generated_at?.slice(0, 10)}.
            </p>
            <ScorecardTiles card={card} />
            <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
              {Object.entries(card.per_scenario).map(([s, b]) => (
                <div key={s} className="flex items-center gap-2 rounded-lg border bg-card px-3 py-2 text-xs">
                  <span className="size-2.5 shrink-0 rounded-sm" style={{ background: SCENARIO[s]?.color }} />
                  <span className="font-medium">{SCENARIO[s]?.label ?? s}</span>
                  <span className="ml-auto text-muted-foreground tabular-nums">
                    {b.events ? `${b.detected}/${b.events} events · ` : "no expected events · "}
                    {b.alerts.toLocaleString()} alerts · PPV {b.ppv == null ? "—" : `${Math.round(b.ppv * 100)}%`} ·{" "}
                    {b.false_alarms_per_week ?? "—"} FA/wk
                  </span>
                </div>
              ))}
            </div>
          </>
        ) : (
          !error && <Skeleton className="h-28 w-full" />
        )}

        {card?.by_tier && (
          <Card>
            <CardHeader><CardTitle className="text-sm">Against chance, by severity tier</CardTitle></CardHeader>
            <CardContent><TierTable card={card} /></CardContent>
          </Card>
        )}

        <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
          <Card>
            <CardHeader><CardTitle className="text-sm">Scenario coverage (recall by event type)</CardTitle></CardHeader>
            <CardContent>{card ? <CoverageHeatmap card={card} /> : <Skeleton className="h-40 w-full" />}</CardContent>
          </Card>
          <Card>
            <CardHeader><CardTitle className="text-sm">Calibration (10 bins)</CardTitle></CardHeader>
            <CardContent>
              {cal ? <CalibrationPlot overall={cal.overall} perScenario={cal.per_scenario} /> : <Skeleton className="h-72 w-full" />}
            </CardContent>
          </Card>
        </div>

        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Ground-truth events</CardTitle>
          </CardHeader>
          <CardContent>{ev ? <EventList events={ev.events} /> : <Skeleton className="h-64 w-full" />}</CardContent>
        </Card>
        <p className="text-xs text-muted-foreground">
          Method, matching rules and caveats: docs/backtest_methodology.md in the repository.
        </p>
      </div>
    </main>
  );
}
