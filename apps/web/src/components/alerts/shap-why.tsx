"use client";

import { Bar, BarChart, Cell, LabelList, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  PILLAR,
  signalLabel,
  useShapWaterfall,
  type AlertDetail,
  type AlertItem,
  type Pillar,
  type WaterfallStep,
} from "@/lib/alerts";

const SURFACE = "var(--popover)"; // 2px gaps between fills = the Sheet surface
const INK = "var(--foreground)";
const MUTED = "var(--muted-foreground)";

export type Lang = "en" | "ar";

/** UI strings for the alert side panel (the narration itself comes from the API). */
export const T = {
  en: {
    why: "Why this alert",
    reliability: "Explanation reliability: high (R² 0.998)",
    template: "Template text (LLM unavailable)",
    action: "Recommended action",
    owner: "Owner",
    window: "Time window",
    escalate: (m: number) => `Escalate if not acknowledged in ${m} min`,
    ack: "Acknowledge",
    acked: "Acknowledged",
    yourRole: "Your role",
  },
  ar: {
    why: "لماذا هذا التنبيه",
    reliability: "موثوقية التفسير: عالية (R² 0.998)",
    template: "نص قالب (النموذج اللغوي غير متاح)",
    action: "الإجراء الموصى به",
    owner: "المسؤول",
    window: "الإطار الزمني",
    escalate: (m: number) => `يُصعَّد إذا لم يُؤكَّد الاستلام خلال ${m} دقيقة`,
    ack: "تأكيد الاستلام",
    acked: "تم تأكيد الاستلام",
    yourRole: "دورك",
  },
} as const;

/** EN / عربي segmented toggle (shadcn Buttons in a group). */
export function LangToggle({ lang, onChange }: { lang: Lang; onChange: (l: Lang) => void }) {
  return (
    <div role="group" aria-label="Explanation language" data-slot="button-group" className="inline-flex rounded-lg border p-0.5">
      {(["en", "ar"] as const).map((l) => (
        <Button
          key={l}
          type="button"
          size="xs"
          variant={lang === l ? "default" : "ghost"}
          aria-pressed={lang === l}
          lang={l}
          onClick={() => onChange(l)}
        >
          {l === "en" ? "EN" : "عربي"}
        </Button>
      ))}
    </div>
  );
}

/** One pattern per pillar: 45° hatch for the 2nd signal of the same pillar. */
function HatchDefs() {
  return (
    <defs>
      {(Object.keys(PILLAR) as Pillar[]).map((p) => (
        <pattern key={p} id={`hatch-${p}`} patternUnits="userSpaceOnUse" width="6" height="6" patternTransform="rotate(45)">
          <rect width="6" height="6" fill={PILLAR[p].hex} fillOpacity={0.45} />
          <rect width="3" height="6" fill={PILLAR[p].hex} />
        </pattern>
      ))}
    </defs>
  );
}

type Seg = { key: string; pillar: Pillar; fill: string; nth: number };

/** Stable segment order + fills for an alert's signals (by SHAP share, then pillar). */
function segments(alert: AlertItem): Seg[] {
  const used: Record<Pillar, number> = { env: 0, occ: 0, clin: 0 };
  return Object.keys(alert.driver_details)
    .sort((a, b) => (alert.drivers[b] ?? 0) - (alert.drivers[a] ?? 0))
    .map((key) => {
      const pillar = alert.driver_details[key]?.pillar ?? "env";
      const nth = used[pillar]++;
      return { key, pillar, nth, fill: nth === 0 ? PILLAR[pillar].hex : `url(#hatch-${pillar})` };
    });
}

function pctLabel(v: unknown) {
  const n = Number(v);
  return n >= 6 ? `${Math.round(n)}%` : "";
}

function ContributionBars({ alert, rtl }: { alert: AlertItem; rtl: boolean }) {
  const segs = segments(alert);
  const [start, end]: [[number, number, number, number], [number, number, number, number]] = rtl
    ? [[0, 4, 4, 0], [4, 0, 0, 4]]
    : [[4, 0, 0, 4], [0, 4, 4, 0]];
  const shapLabel = alert.drivers_method === "shap" ? "SHAP · this alert" : "Fusion weights (fallback)";
  const rows = [
    { row: shapLabel, ...alert.drivers },
    { row: "Fusion weights", ...alert.drivers_prior },
  ];
  return (
    <div>
      <div dir="ltr" className="h-[104px]">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart accessibilityLayer={false} data={rows} layout="vertical" margin={{ top: 14, right: 4, bottom: 0, left: 0 }} barCategoryGap={18}>
            <HatchDefs />
            <XAxis type="number" domain={[0, 100]} hide reversed={rtl} />
            <YAxis type="category" dataKey="row" width={112} orientation={rtl ? "right" : "left"} tick={{ fontSize: 11, fill: MUTED }} axisLine={false} tickLine={false} />
            <Tooltip
              cursor={{ fill: "var(--muted)", opacity: 0.4 }}
              contentStyle={{ fontSize: 11 }}
              formatter={(v, k) => [`${Number(v).toFixed(1)}%`, signalLabel(String(k))]}
            />
            {segs.map((s, i) => (
              <Bar
                key={s.key}
                dataKey={s.key}
                name={s.key}
                stackId="d"
                fill={s.fill}
                stroke={SURFACE}
                strokeWidth={2}
                radius={i === segs.length - 1 ? end : i === 0 ? start : 0}
                isAnimationActive={false}
              >
                <LabelList dataKey={s.key} position="top" offset={3} formatter={pctLabel} style={{ fontSize: 10, fill: INK }} />
              </Bar>
            ))}
          </BarChart>
        </ResponsiveContainer>
      </div>
      <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
        {segs.map((s) => (
          <span key={s.key} className="flex items-center gap-1">
            <svg width="10" height="10" aria-hidden>
              <HatchDefs />
              <rect width="10" height="10" rx="2" fill={s.fill} />
            </svg>
            <span className="text-foreground">{signalLabel(s.key)}</span>
            <span className="tabular-nums">
              {(alert.drivers[s.key] ?? 0).toFixed(0)}% vs {(alert.drivers_prior[s.key] ?? 0).toFixed(0)}%
            </span>
          </span>
        ))}
      </div>
    </div>
  );
}

type WfRow = WaterfallStep & { label: string; range: [number, number]; fill: string };

function Waterfall({ alert, rtl }: { alert: AlertDetail; rtl: boolean }) {
  const { data, error, isLoading } = useShapWaterfall(alert.id, alert.drivers_method === "shap");
  if (alert.drivers_method !== "shap") return null;
  if (isLoading) return <Skeleton className="h-36 w-full" />;
  if (error || !data) return <div className="text-xs text-muted-foreground">Waterfall unavailable.</div>;

  const fillOf = new Map(segments(alert).map((s) => [s.key, s.fill]));
  const rows: WfRow[] = data.steps.map((s) => ({
    ...s,
    label: signalLabel(s.signal),
    range: [Math.min(s.baseline, s.cumulative), Math.max(s.baseline, s.cumulative)],
    fill: fillOf.get(s.signal) ?? PILLAR[s.pillar ?? "env"].hex,
  }));
  const xs = [data.base_value, data.prediction, ...rows.flatMap((r) => r.range)];
  const lo = Math.min(...xs);
  const hi = Math.max(...xs);
  const pad = (hi - lo) * 0.18 || 0.05;
  const fmt = (v: number) => (v >= 0 ? "+" : "−") + Math.abs(v).toFixed(2);

  return (
    <div>
      <div className="mb-1 flex items-baseline justify-between text-xs">
        <span className="font-medium">SHAP waterfall</span>
        <span className="text-muted-foreground tabular-nums">
          typical hour {data.base_value.toFixed(2)} → this alert {data.prediction.toFixed(2)}
        </span>
      </div>
      <div dir="ltr" style={{ height: 36 + rows.length * 30 }}>
        <ResponsiveContainer width="100%" height="100%">
          <BarChart accessibilityLayer={false} data={rows} layout="vertical"
            margin={{ top: 16, right: rtl ? 0 : 40, bottom: 0, left: rtl ? 40 : 0 }} barCategoryGap={6}>
            <HatchDefs />
            {/* RTL: the value axis runs right-to-left, so positive (risk-raising) steps point left */}
            <XAxis type="number" domain={[Math.max(0, lo - pad), hi + pad]} tick={{ fontSize: 10, fill: MUTED }}
              tickFormatter={(v: number) => v.toFixed(2)} stroke="var(--border)" reversed={rtl} />
            <YAxis type="category" dataKey="label" width={112} orientation={rtl ? "right" : "left"} tick={{ fontSize: 11, fill: MUTED }} axisLine={false} tickLine={false} />
            <ReferenceLine x={data.base_value} stroke={MUTED} strokeDasharray="3 3"
              label={{ value: "typical", position: "top", fontSize: 10, fill: MUTED }} />
            <ReferenceLine x={data.prediction} stroke={INK}
              label={{ value: "alert", position: "top", fontSize: 10, fill: INK }} />
            <Tooltip
              cursor={{ fill: "var(--muted)", opacity: 0.4 }}
              contentStyle={{ fontSize: 11 }}
              content={({ active, payload }) => {
                const r = active && payload?.[0]?.payload as WfRow | undefined;
                if (!r) return null;
                return (
                  <div className="rounded-md border bg-popover px-2 py-1.5 text-[11px] shadow-sm">
                    <div className="font-medium">{r.label}</div>
                    <div className="tabular-nums">SHAP {fmt(r.shap)} ({r.shap >= 0 ? "raises" : "lowers"} risk)</div>
                    <div className="tabular-nums text-muted-foreground">
                      {r.baseline.toFixed(3)} → {r.cumulative.toFixed(3)}
                      {r.value != null && ` · value ${r.value.toFixed(2)}`}
                    </div>
                  </div>
                );
              }}
            />
            <Bar dataKey="range" isAnimationActive={false} radius={4} stroke={SURFACE} strokeWidth={2}>
              {rows.map((r) => (
                <Cell key={r.signal} fill={r.fill} />
              ))}
              <LabelList dataKey="shap" position={rtl ? "left" : "right"} formatter={(v: unknown) => fmt(Number(v))}
                style={{ fontSize: 10, fill: INK }} />
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
      <p className="mt-1 text-[11px] text-muted-foreground">
        Each bar moves the surrogate&apos;s fused score from a typical hour at these sites (last 90 days) to this alert.
        {alert.shap?.model?.r2_holdout != null && ` Surrogate R² ${alert.shap.model.r2_holdout.toFixed(3)} on held-out hours.`}
      </p>
    </div>
  );
}

/** "Why this alert": LLM narrative (EN / عربي), SHAP vs fusion-weight bar, SHAP waterfall.
 *  The parent sets dir="rtl" on the whole panel for Arabic; charts mirror via `lang`. */
export function ShapWhy({ alert, detail, lang, onLang }: {
  alert: AlertItem; detail: AlertDetail | undefined; lang: Lang; onLang: (l: Lang) => void;
}) {
  const rtl = lang === "ar";
  const t = T[lang];
  const en = alert.explanation_en ?? alert.explanation_template;
  const text = rtl ? alert.explanation_ar ?? en : en;
  const fallback = rtl ? alert.explanation_ar_source !== "llm" : alert.explanation_source !== "llm";
  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-xs uppercase tracking-wider text-slate-500">{t.why}</h3>
        <LangToggle lang={lang} onChange={onLang} />
      </div>
      {text && (
        <p lang={rtl && alert.explanation_ar ? "ar" : "en"} className={rtl ? "text-base leading-loose" : "text-sm leading-relaxed"}>
          {text}
        </p>
      )}
      {text && fallback && <div className="text-[11px] text-muted-foreground">{t.template}</div>}
      <ContributionBars alert={alert} rtl={rtl} />
      {alert.drivers_method !== "shap" && (
        <div className="text-[11px] text-muted-foreground">
          The explanation model could not be fitted for this hex; shares shown are the fusion weights.
        </div>
      )}
      {detail ? <Waterfall alert={detail} rtl={rtl} /> : <Skeleton className="h-36 w-full" />}
      <div className="text-[11px] text-muted-foreground">{t.reliability}</div>
      {detail?.shap_disagreements?.length ? (
        <div className="text-[11px] text-muted-foreground">
          SHAP disagrees with the fusion weights on{" "}
          {detail.shap_disagreements.map((d) => signalLabel(d.signal)).join(", ")}: it is lowering the score right now.
        </div>
      ) : null}
    </section>
  );
}
