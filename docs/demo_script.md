# Demo script: 5 minutes

A recorded 2-minute version of steps 2–6 is in [`docs/media/demo.mp4`](media/demo.mp4).

## Before you go on (10 min before)

```bash
make n8n-up n8n-import         # n8n on :5678 with the routing workflow published
make demo-snapshot             # only if there are no live alerts (quiet season); ~1 min
make demo-api SNAPSHOT=1       # terminal 1 (drop SNAPSHOT=1 if live alerts exist)
make demo-web                  # terminal 2 -> http://localhost:3000
```

Open tabs: dashboard (`localhost:3000`), `/ops`, `/backtest`, n8n (`localhost:5678`, logged in,
Executions tab), the demo channel inbox (https://webhook.site/#!/view/83dbf5dd-7ef6-4a16-bcad-2b0c18b09845,
valid until 2026-11-25), Swagger (`localhost:8000/docs`).

**Fallbacks.** No live alerts: use the snapshot and say so (the dashboard labels it
"Snapshot, not live"). Wi-Fi dies: `/ops` simulates the routing and labels each step
"simulated"; everything else is local. The hosted version is at https://sentinel-grid-health.vercel.app.

---

## 0:00 – 1:00 · The problem (no screen, or the dashboard header)

* UAE summers put hundreds of thousands of outdoor workers in 45–50 °C apparent temperatures,
  and dust events push PM10 past 500 µg/m³. Clinics see heat illness and respiratory cases **after**
  the fact. Site supervisors see absences but not the weather; health authorities see
  encounters but not the sites.
* The signals that would warn early live in three silos: **environment, workforce,
  clinics**. Sentinel Grid fuses them per hexagon, every 15 minutes, and turns a detection into
  **one concrete action for one named role**, in English and Arabic.

## 1:00 – 2:00 · Dashboard tour (`localhost:3000`)

1. **Signal pillars**: clinical (Synthea, synthetic), environmental (live WAQI/Open-Meteo/CAMS),
   occupational (synthetic workforce on real weather). Say the words *synthetic* and *real*.
2. **Live air quality**: real WAQI readings for Dubai and Abu Dhabi right now.
3. **Sites + map**: 15 UAE work-site locations with synthetic rosters: workers on shift,
   absence, and feels-like temperature (real weather).
4. **Alert list**: tick *Include expired* if using the snapshot. Point at the amber banner:
   "this is the real pipeline replayed as of 24 Sep, labelled, not live".

## 2:00 – 3:00 · One alert, explained (click *Respiratory / dust event · Dubai Creek Harbour*)

1. **Why this alert**: the LLM narrative. Every number in it comes from the detector; the
   validator rejects any percentage or decimal that is not in the facts.
2. **SHAP**: this alert's drivers vs the fusion weights (PM2.5 62 % vs a 54 % prior), then the
   waterfall from a typical hour. "Explanation reliability: high (surrogate R² 0.998)".
3. **عربي**: the whole panel flips right-to-left, the charts mirror, and the numbers are identical.
4. **Recommended action**: owner role, time window, escalation timer, from a pydantic-validated
   LLM output (template fallback if invalid).

## 3:00 – 3:45 · From alert to the responsible role (`/ops`, then n8n)

1. Select *Hassyan Power Complex* (owner: safety officer). Rehearse as **Advisory**, which is
   marked "rehearsal" because its true severity is watch.
2. **Replay alert through n8n**: the diagram lights up from n8n's own callbacks:
   *owner notified → wait (10 s in demo, 15 min live) → no ack → owner + site supervisor →
   no ack → emirate operations lead*. The audit log fills on the right.
3. Switch to **n8n** → Executions: the same run, node by node (colour-coded sticky notes per
   role group). Optional: the webhook.site tab shows the messages landing per role.
4. Optional: replay again and click **Acknowledge as owner** within 10 s, and the path stops at
   "acknowledged → done".

## 3:45 – 4:30 · Does it work? Honest backtest (`/backtest`)

1. "We replayed the live detector every 15 minutes over 2023–2024, with assertions that it
   never sees the future, against 11 sourced events."
2. **Against chance, by severity tier** (lead with this, not the tiles):
   * "100 % sensitivity, but randomly re-timed copies of our own alerts also catch 99.7 %.
     So sensitivity tells you nothing here, and we say it."
   * "What is real: alerts land in real event windows **2.2× / 3.2× / 4.7×** more often than chance,
     beyond the 95th percentile, and more so at higher severity."
   * "What is wrong: 123 false alarms a week at watch. Thresholds need a full-year
     recalibration, and that's next."
3. Expand **June 2024 heatwave, Al Ain**: heat-stress score (blue) highest through the window,
   diamonds = alerts. The note shows we corrected the brief's 50.6 °C to NCM's official 49.4 °C.
4. "Heat and dust: validation on real weather. Infectious: plausibility only, because the
   clinical data is synthetic."

## 4:30 – 5:00 · Architecture and close

1. Swagger (`localhost:8000/docs`, or the Azure URL): every number on screen is an API call.
   Try `GET /api/backtest/scorecard`.
2. README architecture diagram: Open-Meteo/CAMS/WAQI + Synthea + workforce → 15-min GitHub
   Actions pipeline (Prophet → PyOD → Bayesian fusion → SHAP → Groq) → FastAPI on Azure →
   Next.js on Vercel. n8n routes, and a weekly backtest keeps the scorecard current.
3. Close on privacy and honesty: aggregate counts with cells under 5 suppressed, no
   patient data in prompts, role titles only, and a methodology doc that lists every limitation
   (`docs/backtest_methodology.md`, `docs/judge_quick_facts.md`).
