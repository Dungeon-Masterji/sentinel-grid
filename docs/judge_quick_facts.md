# Sentinel Grid · judges' quick facts

**One line:** a tri-signal (clinical + environmental + occupational) early-warning system for
UAE population and workforce health. Its alerts explain themselves (SHAP + LLM, English and
Arabic) and are routed to the responsible role (n8n, escalation, audit trail).

Live: https://sentinel-grid-health.vercel.app · `/backtest` · `/ops` · API
https://sentinel-grid-api.azurewebsites.net (`/docs`) · video: `docs/media/demo.mp4`

## Ground truth (backtest 2023-01-01 → 2024-12-31)

| | |
|---|---|
| Events | **11**: 4 heatwaves, 5 dust storms (2 in 2023, 3 in 2024), the April 2024 floods, Ramadan 2024 |
| Sources | every event cites a public record (NCM via The National, Khaleej Times, Gulf News, Aletihad, WAM) |
| Corrections | the brief's "Al Ain 50.6 °C" is a roadside-display photo. The NCM peak for 15-20 June 2024 was 49.4 °C, and the event says so |
| Replay | the live detector, every 15 min (70,176 ticks), models refit daily on past data only; zero lookahead enforced by assertions + tests |

## Headline numbers, with chance baselines

Chance = the same alerts re-timed at random (200 shuffles, ±7–45 days, season kept).

| Alerts counted | Sensitivity | …by chance | PPV | …by chance | **PPV lift** | False alarms/wk |
|---|---|---|---|---|---|---|
| watch+ | 100 % | 99.7 % | 4.6 % | 2.1 % | **2.2×** | 123 |
| advisory+ | 73 % | 95.3 % | 5.1 % | 1.6 % | **3.2×** | 56 |
| warning | 55 % | 76.0 % | 4.7 % | 1.0 % | **4.7×** | 34 |

* **Sensitivity ≈ chance.** Alert volume, not skill, catches the events. We say so.
* **Precision beats chance 2–4.7×**, beyond the 95th percentile of the shuffles at every tier,
  and more so at higher tiers. This is the defensible claim.
* Brier 0.104 vs climatology 0.020 (skill −4.2): the fused score is not a calibrated event probability.
* Watch-tier lead time (66 h) is a matching-window artefact. Advisory/warning median lead is 27–34 h.

## Data provenance

| Pillar | Status |
|---|---|
| Environmental | **REAL**: Open-Meteo ERA5 + forecast, CAMS PM, WAQI live |
| Occupational | **SYNTHETIC**: workforce simulator driven by the real weather |
| Clinical | **SYNTHETIC**: Synthea UAE population. Backtest adds a modest, documented weather coupling |
| Explanations | real SHAP (surrogate R² 0.998) + real Groq LLM output, validated, with template fallback |
| Routing | real n8n workflow; demo channels are public webhook.site inboxes |

Heat/dust backtest = **validation**. Infectious = **plausibility check only**.

## Engineering facts

* Prophet baselines → PyOD (IForest + ECOD + LOF) → Bayesian opinion pool (MAP / PyMC) → 6 h dedupe alert rule
* LightGBM surrogate + TreeSHAP per alert, under 5 ms per alert
* LLM guards: no decimals or percentages absent from the facts; Arabic must keep every number;
  pydantic action schema; urgency bounded by severity; narration ↔ action consistency checks
* Zero-lookahead tests: absurd future data (65 °C, PM10 2,000) changes nothing; a corrupted
  future timestamp makes the replay fail loudly
* 125+ automated tests; CI rebuilds the full backtest weekly (~33 min)

## Privacy

Aggregate catchment counts only, with cells under 5 records suppressed. No patient-level data
reaches any prompt. Role titles only, never names. No phone numbers or chat ids in the repo.

## Limitations (not softened; full list in docs/backtest_methodology.md §8)

* 11 events: one hit moves sensitivity about 9 points; no confidence intervals claimed
* the event lists are incomplete, so FA/week is an upper bound
* emirate-level matching: generous for local events, blind to Al Ain specifically
* ERA5/CAMS are models that smooth station extremes
* thresholds calibrated on autumn 2026, not tuned on the scored window (on purpose)
* synthetic pillars echo the weather, so heat/dust corroboration is an upper bound
* infectious scenario: 53 % of all alerts against zero real infectious events
* hackathon security gaps: unauthenticated acks and replay, shared-token callbacks
