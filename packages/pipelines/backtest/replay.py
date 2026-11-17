"""Replay the live detector over history with zero lookahead.

    replay(start, end) -> ReplayResult(alerts, events, fused, frame)

What "replay" means here. The live pipeline runs generate_alerts(now) every 15 minutes:
it reuses cached models while they are < 24 h old (baseline.refit_after_hours), scores
the last 90 days, fuses them, and applies the alert rule (fusion/alerts.alert_groups +
newest_per_key). Calling generate_alerts 70,000 times for two years is ~8 days of CPU,
so the replay reproduces the same computation per UTC day ("epoch"):

  1. at epoch start E: the panel slice VISIBLE at E (available_at <= E, last 90 days) is
     checked by assert_no_lookahead (every ts_utc AND observed_at < E), then the same
     Prophet baselines + PyOD detectors are fitted on it (detect.fit_all). They are used
     for the whole day, exactly like the live 24 h model cache.
  2. the fusion pool's sigma (combine.fit_sigma_map) is fitted on rows visible at E only
     (last ``sigma_days``), then frozen for the day. Live refits it every run on all
     rows <= now; freezing it per day is the one deliberate simplification.
  3. rows that become visible during the day are scored with those frozen models. Every
     transform is row-wise or trailing (Prophet predict, residual z, PyOD score, trailing
     24 h counts, closed='left' rolling windows), so a row's score does not depend on
     later rows. Hexes the models have never seen are dropped rather than normalised
     with their own (future-inclusive) statistics.
  4. every 15 minutes t in the day: the fused rows with available_at <= t are checked by
     assert_no_lookahead(t) and passed to the SAME alert rule as live. Data are hourly,
     so the visible set only changes on the hour; ticks at :15/:30/:45 reuse the :00
     result (identical by construction) instead of recomputing it.

An alert's issue time (first_seen) is the first tick it appears at. Alert ids are the
live ids (uuid5 of scenario|hex|window), so an alert spanning midnight merges across
epochs.

Clinical rows are lagged by 48 h in history.py (T-2 rule), every row is published 1 h
after its timestamp, and nothing here reads the clock.
"""
from __future__ import annotations

import logging
import multiprocessing as mp
import os
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass

import numpy as np
import pandas as pd

from packages.pipelines.backtest.events import EVENTS_YAML, load_events, match, matching_params
from packages.pipelines.backtest.history import (BACKTEST_DIR, PUBLISH_LATENCY, load_history)
from packages.pipelines.fusion.alerts import (SEVERITY_RANK, _eligible, _severity, alert_groups,
                                              alert_id, newest_per_key, unit_signal_frame)
from packages.pipelines.fusion.combine import _prep, fit_sigma_map, fuse
from packages.pipelines.fusion.config import SCENARIOS_YAML, load_scenarios
from packages.pipelines.fusion.detect import detect, fit_all
from packages.pipelines.fusion.signals import PANEL_COLUMNS

log = logging.getLogger("sentinel.backtest.replay")
TICK = pd.Timedelta(minutes=15)
DAY = pd.Timedelta(days=1)


class LookaheadError(AssertionError):
    """Raised when a replay would let the detector see data from at or after as_of."""


def assert_no_lookahead(df: pd.DataFrame, as_of, what: str = "data slice") -> None:
    """as_of must be STRICTLY later than every ts_utc and observed_at in ``df``."""
    as_of = pd.Timestamp(as_of)
    if df is None or len(df) == 0:
        return
    for col in ("ts_utc", "observed_at"):
        if col not in df.columns:
            continue
        bad = df[col] >= as_of
        if bad.any():
            ex = df.loc[bad].iloc[0]
            raise LookaheadError(
                f"zero-lookahead violated in {what} at as_of={as_of}: {int(bad.sum())} row(s) with "
                f"{col} >= as_of (e.g. signal={ex.get('signal', ex.get('scenario_id'))} "
                f"h3={ex.get('h3', ex.get('unit_h3'))} {col}={ex[col]}, "
                f"available_at={ex.get('available_at')})")


def visible(panel: pd.DataFrame, as_of, history_days: float | None = None) -> pd.DataFrame:
    """Rows published by ``as_of`` (available_at <= as_of), asserted lookahead-free."""
    as_of = pd.Timestamp(as_of)
    v = panel[panel["available_at"] <= as_of]
    if history_days is not None:
        v = v[v["ts_utc"] >= as_of - pd.Timedelta(days=history_days)]
    assert_no_lookahead(v, as_of, "visible panel")
    return v


def _known_hexes(rows: pd.DataFrame, fitted: dict) -> pd.DataFrame:
    keep = np.zeros(len(rows), dtype=bool)
    sig, h3 = rows["signal"].to_numpy(), rows["h3"]
    for name, fs in fitted.items():
        keep |= (sig == name) & h3.isin(set(fs.baseline.hex_stats)).to_numpy()
    return rows[keep]


def alerts_at(fused_visible: pd.DataFrame, as_of: pd.Timestamp, hours: int) -> list[dict]:
    """The live alert rule (alert_groups + newest_per_key) on the visible fused rows."""
    out = []
    for sid, cell, w0, expires, g, peak, sev in alert_groups(fused_visible, as_of, hours):
        out.append({"id": alert_id(sid, cell, w0), "scenario_id": sid, "h3_cell": cell,
                    "window_start": w0, "expires_at": expires, "severity": sev,
                    "score": float(peak["mean"]), "peak_ts": peak["ts_utc"]})
    return newest_per_key(out)


@dataclass
class EpochResult:
    day: pd.Timestamp
    alerts: pd.DataFrame
    fused: pd.DataFrame
    seconds: float
    notes: list


def run_epoch(day, panel: pd.DataFrame, units: pd.DataFrame, cfg: dict, *,
              sigma_days: int = 30) -> EpochResult:
    t0 = time.time()
    E = pd.Timestamp(day).tz_convert("UTC").floor("D")
    hours = int(cfg.get("fusion", {}).get("dedupe_window_hours", 6))
    hist_days = int(cfg["spatial"].get("history_days", 90))
    prior = tuple(cfg.get("fusion", {}).get("prior", [2, 2]))
    min_cov = float(cfg.get("fusion", {}).get("min_coverage", 0.4))
    notes = []

    # 1. models from what is visible at E (asserted inside visible())
    vis = visible(panel, E, hist_days)
    fitted = fit_all(cfg, vis[PANEL_COLUMNS], E, cache_dir=None, use_cache=False)
    if not fitted:
        return EpochResult(E, pd.DataFrame(), pd.DataFrame(), time.time() - t0, ["no models"])

    # 2-3. score sigma history + today's rows with the frozen models
    lo = E - pd.Timedelta(days=sigma_days)
    rows = panel[(panel["available_at"] <= E + DAY) & (panel["ts_utc"] >= lo)]
    rows = _known_hexes(rows, fitted)
    scored = detect(rows[PANEL_COLUMNS], cfg, E + DAY, fitted=fitted)
    frame = unit_signal_frame(scored, units, cfg)
    if frame.empty:
        return EpochResult(E, pd.DataFrame(), pd.DataFrame(), time.time() - t0, ["empty frame"])
    frame = frame.sort_values(["unit_h3", "ts_utc"]).reset_index(drop=True)
    frame_avail = frame["ts_utc"] + PUBLISH_LATENCY
    hist = (frame_avail <= E).to_numpy()
    need = (frame["ts_utc"] >= E - pd.Timedelta(hours=2 * hours + 1)).to_numpy()
    eligible = _eligible(frame, cfg)

    parts = []
    for sc in cfg["scenarios"]:
        weights = {s["name"]: float(s["weight"]) for s in sc["signals"]}
        S = pd.DataFrame({k: frame[f"{k}__score"] if f"{k}__score" in frame else np.nan
                          for k in weights}, index=frame.index)
        _, X, M, w = _prep(S[hist], weights)
        sigma = fit_sigma_map(X, M, w, prior=prior)
        res = fuse(S[need], weights, mode="map", prior=prior, sigma=sigma)
        res["scenario_id"] = sc["id"]
        res["unit_h3"] = frame.loc[need, "unit_h3"].to_numpy()
        res["ts_utc"] = frame.loc[need, "ts_utc"].to_numpy()
        ok = eligible[need] & (res["coverage"].to_numpy(float) >= min_cov)
        res["eligible"] = ok
        res["severity"] = [(_severity(m, sc["thresholds"]) if e else None)
                           for m, e in zip(res["mean"].to_numpy(float), ok)]
        parts.append(res.reset_index(drop=True))
    fused = pd.concat(parts, ignore_index=True)
    fused["ts_utc"] = pd.to_datetime(fused["ts_utc"], utc=True)
    fused["available_at"] = fused["ts_utc"] + PUBLISH_LATENCY
    fused["observed_at"] = fused["ts_utc"]      # latest underlying observation (clinical is older)

    # 4. 15-minute ticks through the day
    state: dict[str, dict] = {}
    last_key, current = None, []
    for k in range(int(DAY / TICK)):
        t = E + k * TICK
        v = fused[fused["available_at"] <= t]
        assert_no_lookahead(v, t, "fused rows at tick")
        key = (v["ts_utc"].max() if len(v) else None, t.floor(f"{hours}h"))
        if key != last_key:
            current = alerts_at(v, t, hours)
            last_key = key
        for a in current:
            s = state.get(a["id"])
            if s is None:
                state[a["id"]] = {**{x: a[x] for x in ("id", "scenario_id", "h3_cell", "window_start",
                                                      "expires_at", "peak_ts")},
                                  "first_seen": t, "last_seen": t, "severity_first": a["severity"],
                                  "severity_max": a["severity"], "score_first": a["score"],
                                  "score_max": a["score"], "ticks": 1,
                                  **{f"first_seen_{tier}": (t if SEVERITY_RANK[a["severity"]] >= r else pd.NaT)
                                     for tier, r in SEVERITY_RANK.items()}}
            else:
                s["last_seen"] = t
                s["ticks"] += 1
                for tier, r in SEVERITY_RANK.items():
                    if SEVERITY_RANK[a["severity"]] >= r and pd.isna(s[f"first_seen_{tier}"]):
                        s[f"first_seen_{tier}"] = t
                if SEVERITY_RANK[a["severity"]] > SEVERITY_RANK[s["severity_max"]]:
                    s["severity_max"] = a["severity"]
                if a["score"] > s["score_max"]:
                    s["score_max"], s["peak_ts"] = a["score"], a["peak_ts"]
    day_rows = fused[(fused["ts_utc"] >= E) & (fused["ts_utc"] < E + DAY)]
    keep = ["scenario_id", "unit_h3", "ts_utc", "mean", "ci_low", "ci_high", "coverage",
            "eligible", "severity", "sigma"]
    return EpochResult(E, pd.DataFrame(list(state.values())), day_rows[keep].reset_index(drop=True),
                       time.time() - t0, notes)


def merge_alerts(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Same alert id seen in two epochs (spans midnight) -> one record."""
    df = pd.concat([f for f in frames if len(f)], ignore_index=True) if any(len(f) for f in frames) \
        else pd.DataFrame(columns=["id", "scenario_id", "h3_cell", "window_start", "expires_at",
                                   "peak_ts", "first_seen", "last_seen", "severity_first",
                                   "severity_max", "score_first", "score_max", "ticks",
                                   "first_seen_watch", "first_seen_advisory", "first_seen_warning"])
    if df.empty:
        for c in ("window_start", "expires_at", "peak_ts", "first_seen", "last_seen",
                  "first_seen_watch", "first_seen_advisory", "first_seen_warning"):
            df[c] = pd.to_datetime(df[c], utc=True)
        return df
    df = df.sort_values("first_seen")
    rank = df["severity_max"].map(SEVERITY_RANK)
    agg = df.assign(_r=rank).groupby("id", sort=False).agg(
        scenario_id=("scenario_id", "first"), h3_cell=("h3_cell", "first"),
        window_start=("window_start", "first"), expires_at=("expires_at", "first"),
        first_seen=("first_seen", "min"), last_seen=("last_seen", "max"),
        severity_first=("severity_first", "first"), score_first=("score_first", "first"),
        score_max=("score_max", "max"), ticks=("ticks", "sum"), _r=("_r", "max"),
        first_seen_watch=("first_seen_watch", "min"), first_seen_advisory=("first_seen_advisory", "min"),
        first_seen_warning=("first_seen_warning", "min"))
    inv = {v: k for k, v in SEVERITY_RANK.items()}
    agg["severity_max"] = agg.pop("_r").map(inv)
    return agg.reset_index().sort_values("first_seen").reset_index(drop=True)


# -- parallel driver -----------------------------------------------------------------------
_G: dict = {}


def _job(day):
    return run_epoch(day, _G["panel"], _G["units"], _G["cfg"], sigma_days=_G["sigma_days"])


@dataclass
class ReplayResult:
    alerts: pd.DataFrame          # one row per alert + matched / matched_event_ids
    events: pd.DataFrame          # one row per event + detected / lead_time_hours / ...
    fused: pd.DataFrame           # hourly fused scores (scenario x hex x hour), for calibration
    start: pd.Timestamp
    end: pd.Timestamp
    timings: dict

    @property
    def weeks(self) -> float:
        return (self.end - self.start) / pd.Timedelta(weeks=1)

    def to_frame(self) -> pd.DataFrame:
        """Matched + unmatched alerts and matched + unmatched events in one frame."""
        a = self.alerts.assign(kind="alert", time=self.alerts.get("first_seen"))
        e = self.events.rename(columns={"detected": "matched"}).assign(kind="event", time=self.events["start"])
        cols = ["kind", "id", "time", "matched"]
        return pd.concat([a[cols + ["scenario_id", "h3_cell", "severity_first", "severity_max",
                                    "matched_event_ids"]],
                          e[cols + ["event_type", "label", "lead_time_hours"]]],
                         ignore_index=True)


def replay(start, end, scenarios_yaml=SCENARIOS_YAML, events_yaml=EVENTS_YAML, *,
           history_dir=BACKTEST_DIR, panel: pd.DataFrame | None = None,
           units: pd.DataFrame | None = None, workers: int | None = None,
           sigma_days: int = 30) -> ReplayResult:
    """Replay every 15-minute tick in [start, end) and match the alert stream to events."""
    cfg = load_scenarios(scenarios_yaml)
    start = pd.Timestamp(start).tz_localize("UTC") if pd.Timestamp(start).tzinfo is None \
        else pd.Timestamp(start).tz_convert("UTC")
    end = pd.Timestamp(end).tz_localize("UTC") if pd.Timestamp(end).tzinfo is None \
        else pd.Timestamp(end).tz_convert("UTC")
    hist_days = int(cfg["spatial"].get("history_days", 90))
    t0 = time.time()
    if panel is None:
        panel, units = load_history(start - pd.Timedelta(days=hist_days + 2), end, history_dir)
    days = list(pd.date_range(start.floor("D"), end, freq="D", inclusive="left"))
    workers = workers or max(1, min(len(days), (os.cpu_count() or 2)))
    log.info("replay %s..%s: %d epochs x 96 ticks, %d worker(s)", start.date(), end.date(),
             len(days), workers)
    _G.update(panel=panel, units=units, cfg=cfg, sigma_days=sigma_days)
    results: list[EpochResult] = []
    if workers == 1:
        for d in days:
            results.append(_job(d))
    else:
        ctx = mp.get_context("fork") if "fork" in mp.get_all_start_methods() else None
        with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
            for i, r in enumerate(ex.map(_job, days, chunksize=1)):
                results.append(r)
                if (i + 1) % 30 == 0:
                    log.info("replayed %d/%d days (%.0fs)", i + 1, len(days), time.time() - t0)
    alerts = merge_alerts([r.alerts for r in results])
    alerts = alerts[(alerts["first_seen"] >= start) & (alerts["first_seen"] < end)] if len(alerts) else alerts
    fused = pd.concat([r.fused for r in results if len(r.fused)], ignore_index=True)
    events = load_events(events_yaml, units)
    events = events[(events["end"] >= start) & (events["start"] < end)].reset_index(drop=True)
    mp_ = matching_params(events_yaml)
    alerts, events = match(alerts, events, lead_window_hours=mp_["lead_window_hours"],
                           grace_hours=mp_["grace_hours"])
    timings = {"total_s": round(time.time() - t0, 1), "epochs": len(days),
               "epoch_s_mean": round(float(np.mean([r.seconds for r in results])), 2) if results else None,
               "workers": workers}
    return ReplayResult(alerts, events, fused, start, end, timings)
