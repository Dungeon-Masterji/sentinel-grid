# Sentinel Grid

## Backtest scorecard (2023–2024, honest numbers)

The live detector replayed every 15 minutes over 2023-01-01 → 2024-12-31 with zero
lookahead, scored against 11 sourced UAE events (4 heatwaves, 5 dust storms, the April 2024
floods, Ramadan 2024). Chance = the same alerts re-timed at random (200 seasonal shuffles).

| Alerts counted | Sensitivity | …by chance | PPV | …by chance | PPV lift | False alarms / week | Median lead |
|---|---|---|---|---|---|---|---|
| watch or higher | **100 %** (11/11) | 99.7 % | 4.6 % | 2.1 % | 2.2× | 123 | 66 h |
| advisory or higher | 73 % (8/11) | 95.1 % | 5.2 % | 1.6 % | 3.3× | 56 | 20 h |
| warning | 55 % (6/11) | 74.5 % | 4.5 % | 1.0 % | **4.5×** | 34 | 34 h |

Brier score 0.105 (always-predict-the-base-rate: 0.020, skill −4.2).

**Read it this way:** the 100 % sensitivity is an artefact of alert volume, since randomly timed
copies of the same alerts score 99.7 %. The real, statistically clear signal is precision: alerts
fall inside real event windows 2–4.5× more often than chance, more so at higher tiers. False
alarms are far too high for operations, and the thresholds need a full-year recalibration.
Environmental data are real (ERA5 + CAMS); clinical and occupational pillars are synthetic and
driven by that weather, so heat/dust results are a validation and infectious results a
plausibility check only. Full method, matching rules and caveats:
[docs/backtest_methodology.md](docs/backtest_methodology.md) · dashboard: `/backtest` ·
API: `GET /api/backtest/scorecard`.

Tri-signal early-warning for population and workforce health in the UAE — fusing clinical, environmental, and occupational signals into explainable, privacy-preserving alerts.

Status: scaffolding complete. Pipelines wiring in progress.
