# Backtest methodology

**What this is:** the *live* Sentinel Grid detector (Prophet baselines → PyOD ensemble →
Bayesian fusion → the 6-hour dedup alert rule) replayed over every 15-minute tick of
2023-01-01 → 2024-12-31. Its alert stream is scored against public records of real UAE
events.

**What this is not:** a clinical validation. Read the next section before any number.

## 1. The honest version first

Sentinel Grid fuses three pillars. In this backtest they have very different standing:

| Pillar | Data in the backtest | Real? | What the backtest can claim |
|---|---|---|---|
| Environmental | ERA5 reanalysis (Open-Meteo archive) + CAMS global PM (Open-Meteo air-quality), hourly, at the 15 site points | **Real** | Heat and dust detection is a **genuine validation** against independently recorded events |
| Occupational | Simulated workforce (`synth/workforce.py`) whose absence model is driven by the real historical weather | Synthetic, **real driver** | Shows the pipeline propagates a real hazard through a *modelled* absence response. It is not evidence about real workers |
| Clinical | One Synthea run of a 10,000-person UAE population, plus a modest weather coupling that adds heat/respiratory encounters on real hot/dusty days | Synthetic | **Plausibility check only.** Real ground truth exists only for the environmental side |

Consequences you should read the scorecard with:

* **Heatwaves and dust storms:** a rigorous test. The detector sees real weather and does
  not know the event dates. If it fires, it fired on real physical signal.
* **Infectious clusters and clinical escalation:** there is no real outbreak record in this
  backtest. Synthea's febrile module is seasonal but knows nothing about real 2023-2024
  epidemiology, so `coverage_matrix[infectious, *]` and every infectious-scenario alert
  measure *internal consistency*, not real-world skill. Infectious alerts count as false
  alarms in PPV whenever they fall outside a listed event, and almost all of them do.
* **Circularity warning:** the occupational and clinical pillars partly echo the weather
  they were generated from. The heat-stress and dust scenarios therefore get some
  "corroboration" that is really the environmental signal seen twice. Treat their numbers
  as an **upper bound** on what real, independent pillars would give.
* **Why synthetic clinical does not invalidate the backtest:** the environmental pillar carries
  most of the weight for the two scenarios with real ground truth (heat: 0.45 environmental
  + 0.35 occupational, dust: 0.60 environmental). Their clinical inputs are aggregate catchment counts whose
  rate responds to the real weather only through the documented, modest coupling below.
  The scorecard's per-scenario rows let you read heat/dust separately from infectious.

## 2. Events (configs/ground_truth_events.yaml)

Chosen because they are the best-documented UAE hazards of 2023-2024 that the scenarios
are meant to catch, or that a reviewer would ask about even though no scenario models them
(floods, Ramadan). Every event cites a public source. Where the brief and the record
disagreed, the record won:

* **June 2024 heatwave.** The brief's "Al Ain 50.6 °C" traces to a photo of a roadside
  display in Al Shawamekh. The official NCM peak for 15-20 June was 49.4 °C (Sweihan, 16
  June), so a second event covers 21-25 June (Um Azimul 50.3 °C, NCM). ERA5, which is what
  the detector sees, peaks at 47.7 °C air temperature over Al Ain: reanalysis smooths
  station extremes.
* **April 2024 floods** (254 mm in <24 h at Khatm Al Shakla, NCM). No scenario models floods.
  It is mapped to the respiratory and infectious scenarios as a plausibility check, and a
  miss is the expected result.
* **Dust storms** (2023: 10 Jan, 8 Mar; 2024: 20 Mar, 28 Jul, 5 Nov). Only dates whose source
  clearly dates the storm were kept.
* **Ramadan 2024** (11 Mar - 9 Apr). This is a month-long exposure, not an acute event. March/April
  apparent temperatures rarely pass the 38 °C heat floor, and no signal models fasting, so
  the heat scenario is not expected to fire. It is kept to show that, not hidden.
* **Spatial granularity:** all 15 sites are in Abu Dhabi or Dubai emirate, and none is in Al Ain or
  deep Al Dhafra. Events are therefore matched at emirate level (`affected_hexes: auto`).
  That is generous to the detector for localised events (e.g. Madinat Zayed dust).

## 3. Data and sources

| Signal | Source | Gaps |
|---|---|---|
| apparent temperature | Open-Meteo archive (ERA5) | none in window |
| PM2.5, PM10 | Open-Meteo air-quality, `cams_global` domain (from 2022-08) | CAMS is a model, not a monitor. **WAQI has no public historical API** (its feed is current-only), so it cannot backfill. **OpenAQ v3** fills CAMS gaps only when `OPENAQ_API_KEY` is set (v3 requires a key). Without one, gaps stay NaN and are never imputed |
| absence rates | `workforce.generate_shift_log` on the real site weather, one seed per month | synthetic |
| clinical counts | Synthea (reference date 2 days after the window, so the end-of-run taper falls outside it). The febrile module, gated to ≥ 2025 in the live config, is started at the window's first year for this run only | synthetic |

**Weather coupling (clinical).** Synthea modules do not see real weather. Per catchment city
and local day:
* **heat:** extra heat-related encounters ~ Poisson(city mean daily rate × min(1.0, 0.10 × (max
  apparent temperature − 40 °C)⁺));
* **dust:** extra respiratory encounters ~ Poisson(city rate × min(0.8, 0.01 × (mean PM10 − 100)⁺)).

Each city-day has its own seed derived from the date. Even doubled, heat counts stay small,
so many catchment-hours fall under the 5-record suppression rule and drop out (that is the
privacy rule working, not a bug).

**Clinical lateness (the T-2 rule).** Synthea data taper in the last two days of a run. The replay
treats day T-2 as the latest reliable clinical observation, so every clinical value is
shifted 48 h: the value the detector sees at hour *t* is the trailing-24 h count that ended
at *t − 48 h*. Clinical evidence therefore can never give lead time in this backtest.

## 4. The replay and zero lookahead

`packages/pipelines/backtest/replay.py`. Every row of the historical panel carries
`observed_at` (when the observation happened) and `available_at` (when the replay may see
it: `ts + 1 h` for every row, after the 48 h clinical shift).

* **Per UTC day (epoch E):** the slice with `available_at <= E` over the previous 90 days is
  passed through `assert_no_lookahead(slice, E)`. That assertion raises `AssertionError` if
  any `ts_utc` **or** `observed_at` is `>= E` (strictly before, never equal). The same
  Prophet + PyOD models as the live pipeline are fitted on it. As live, models are reused
  for 24 h.
* **Fusion `sigma`** is fitted on rows visible at E (last 30 days) and frozen for the day.
  Live refits it every run, and this per-day freeze is the one deliberate simplification.
* **Every 15 minutes** the visible fused rows are asserted again and passed to the **same
  function** the live pipeline uses (`fusion/alerts.alert_groups` + `newest_per_key`), so
  dedupe windows, alert ids and severities are identical by construction. Data are hourly,
  so the visible set only changes on the hour. The :15/:30/:45 ticks reuse that result
  rather than recompute it, and an alert's issue time is the first tick it appears at.
* Each row's score depends only on that row and on models trained before E. Prophet predict,
  residual z, PyOD score, trailing 24 h counts and `closed='left'` rolling windows are all
  row-wise or trailing. Hexes a model has never seen are dropped rather than normalised
  with future-inclusive statistics.

Tests (`tests/test_backtest.py`):
* the live `generate_alerts(as_of=T)` is run on a world whose data continue past T with
  absurd values (65 °C, PM10 2,000 µg/m³, 500 heat encounters). Spies on `fit_all`/`detect`
  assert nothing after T reaches them, and the alerts are identical to the truncated world;
* the replay gives identical alerts when absurd rows are appended after the window;
* a corrupted history file (a row published "in the past" with a 2030 timestamp) makes the
  replay **fail loudly** with `zero-lookahead violated`.

## 5. What counts as a match

An alert matches an event when **all three** hold:

1. **scenario:** the alert's scenario is in `scenario_map[event_type]` (heatwave and
   Ramadan → heat-stress; dust → respiratory/dust; flood → respiratory + infectious;
   heat+dust → both);
2. **space:** the alert's hex is one of the event's hexes;
3. **time:** the alert was *first issued* between `start − 72 h` and `end + 12 h`.

Overlapping events (a dust storm inside a heatwave) are scored independently: one alert can
match two events if its scenario maps to both.

## 6. Metrics

* **lead_time_hours** = event start − first matching alert's issue time. Positive means warned ahead.
  Negative means the first alert came after the start. Missed events have none.
* **sensitivity** = detected events / events.
* **PPV** = matched alerts / all alerts. Every alert counts, including infectious alerts that
  have no real events to match, which is why PPV is also reported per scenario.
* **false alarms / week** = unmatched alerts / weeks replayed. Every alert counts once,
  however long it stays active.
* **Brier** = mean (p − y)² over eligible scenario × hex × hour rows, where p = the fused score and y = 1
  inside an event window for a scenario expected to catch it. It is reported next to the
  climatology Brier (always predict the base rate) and the skill score. The fused score is
  P(scenario firing) under the fusion model, not P(a listed event is under way), so poor
  calibration against this target is expected and shown, not tuned away.
* **coverage matrix** = for every scenario × event type, the fraction of events of that type with ≥ 1
  matching alert from that scenario, whether expected or not. "No events" is shown as such,
  never as 0 %.

## 7. Results

See the scorecard at the top of `README.md` and `/backtest` on the dashboard. The run
writes the numbers to `data/processed/backtest/results/scorecard.json`.

Run: 2023-01-01 → 2024-12-31 (104.4 weeks, 731 daily model refits, 70,176 fifteen-minute
ticks), 11 events in window. These are the numbers of the weekly CI run of 2026-11-17 that
the live API serves. Every run regenerates the synthetic pillars, so they move slightly;
a local run the same day gave PPV lifts of 2.2× / 3.3× / 4.5×. Chance = the same alerts re-timed at random, 200 shuffles
("seasonal": ±7–45 days, so the season is kept; "uniform" in scorecard.json).

| Alerts counted | Alerts | False alarms/wk | Sensitivity | …chance | PPV | …chance (p95) | PPV lift | Median lead |
|---|---|---|---|---|---|---|---|---|
| watch or higher | 13,484 | 123.2 | 100 % (11/11) | 99.7 % | 4.6 % | 2.1 % (2.5 %) | 2.2× | 66 h* |
| advisory or higher | 6,155 | 55.9 | 73 % (8/11) | 95.3 % | 5.1 % | 1.6 % (2.0 %) | 3.2× | 27 h |
| warning | 3,734 | 34.1 | 55 % (6/11) | 76.0 % | 4.7 % | 1.0 % (1.3 %) | 4.7× | 34 h |

Brier 0.104 vs 0.020 for always predicting the base rate (skill −4.2).

**What this says, plainly:**

1. **The headline 100 % sensitivity is worthless.** The detector raises ~130 alerts a week
   across 15 hexes and 3 scenarios, so randomly re-timed copies of its own alerts catch
   99.7 % of events too. At advisory and warning tiers the real stream does *worse* than
   chance on sensitivity, because real alerts come in bursts that cover fewer distinct
   events than scattered random ones.
2. **There is a real signal, and it is in precision.** Alerts land inside real event
   windows 2.2× (watch), 3.2× (advisory) and 4.7× (warning) more often than randomly
   re-timed copies, above the 95th percentile of 200 shuffles at every tier. Higher tiers
   are more event-specific. That is the one claim this backtest supports.
3. **Lead time is not meaningful at watch tier.** \*With near-continuous alerting, the "first
   matching alert" is simply the first one inside the 72 h lead window, which is why
   watch-tier leads cluster at 63–71 h. Advisory/warning leads (27–34 h median) are less
   contaminated but still inflated by volume.
4. **The thresholds are mis-set for the full year.** They were calibrated on autumn 2026 to an
   alert budget of ~3 % / 0.7 % / 0.15 % of hex-hours; summer heat and spring dust blow
   straight through that budget. 34 warnings a week is not operable.
5. **The fused score is not a calibrated event probability.** Observed event frequency rises
   to ~3.4 % at fused ≈ 0.34 and then *falls* to 0 above 0.7: the highest scores occur
   outside listed events (unlisted hot/dusty days, and infectious-scenario noise from
   the synthetic clinical pillar).
6. **Infectious scenario:** 7,174 alerts (53 % of all) against zero real infectious events, which is
   pure false-alarm load from the synthetic pillar. Its "detections" of heatwaves (75 %)
   and dust (80 %) in the coverage matrix are chance, as the shuffles show.
7. **Ramadan** counts as "detected" only because a month-long window guarantees an overlap;
   its first alert came 167 h *after* the start. **The flood** was "detected" by 314 alerts
   in its window, which is chance as well. No scenario models floods.

**What would fix it (Chunk 7+):** re-calibrate thresholds on a full year with a
seasonal baseline (Prophet `yearly_seasonality`, off today because the live window is
90 days), require multi-pillar agreement before advisory, cap alerts per hex per day,
and gate the infectious scenario off until a real clinical feed exists.

## 8. Known limitations

* **Small event set.** 11 events, about 4 per type. One extra hit moves sensitivity by about 9 points.
  Confidence intervals would be wide, and none are claimed.
* **The event lists are incomplete.** Hot days and dusty hours outside the listed events are
  real hazards, but every alert they trigger is counted as a false alarm. FA/week is
  therefore an upper bound on true false alarms.
* **Emirate-level matching** is generous for local events and blind to Al Ain specifically.
* **CAMS and ERA5 are models.** They smooth station extremes (ERA5 47.7 °C vs NCM 49.4 °C
  in June 2024) and may time dust plumes differently from visibility reports.
* **The thresholds were calibrated on autumn 2026 data** (`scenarios.yaml`) and not re-tuned
  for this backtest. That is deliberate: tuning on the scored window would leak.
* **Synthetic pillars echo the weather** (section 1). Real clinical and HR feeds would be
  noisier and only partly correlated, so expect lower corroboration than shown here.
* **Daily `sigma` freeze** (section 4) differs from the live per-run refit.
