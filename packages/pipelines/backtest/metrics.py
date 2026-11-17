"""Backtest metrics: lead time, sensitivity, PPV, false alarms, calibration, coverage.

Definitions (also in docs/backtest_methodology.md):

  per event
    lead_time_hours           event start - first matching alert's issue time (first_seen).
                              Positive = warned ahead; negative = first alert came after
                              the event had started; None = missed.
    alert_severity_at_first_hit, peak_severity_during_event
  whole window
    sensitivity               detected events / events                (recall)
    ppv                       matched alerts / all alerts             (precision)
    false_alarms_per_week     unmatched alerts / weeks replayed
    mean_lead_time_hours      over detected events
    brier                     mean (p - y)^2 over eligible scenario x hex x hour rows,
                              p = fused score, y = 1 inside an event window (that hex,
                              a scenario expected for that event type). Reported with the
                              climatology Brier (always predict the base rate) and the
                              skill score 1 - brier / brier_climatology.
  by_tier                     the same metrics counting only alerts that reached watch /
                              advisory / warning, each timed from when it FIRST reached that
                              tier (first_seen_<tier>), not from its earlier, lower tier.
  chance baselines            each scenario x hex alert series is circularly shifted by a
                              random offset and re-scored, n times: same alert count, rhythm
                              and places, random timing. "uniform": 30 days .. span - 30 days
                              (summer alerts can land in winter). "seasonal": +-7..45 days, so
                              the season is kept and only the event timing is scrambled; it
                              asks "does it find the event, or does it just know it's summer?".
                              A metric close to its chance value carries no information.
  coverage_matrix             scenario x event_type -> fraction of events of that type
                              with >= 1 matching alert from that scenario, whether or not
                              the scenario is "supposed" to catch that type.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from packages.pipelines.backtest.events import EVENT_TYPES, hits, match

COVERAGE_TYPES = ("heatwave", "dust_storm", "flood", "ramadan_stress", "infectious_cluster")


def _r(x, nd=3):
    return None if x is None or (isinstance(x, float) and not np.isfinite(x)) else round(float(x), nd)


def event_labels(fused: pd.DataFrame, events: pd.DataFrame) -> np.ndarray:
    """y for each fused row: inside any event window, at an event hex, for a scenario
    expected to catch that event type."""
    y = np.zeros(len(fused), dtype=bool)
    if fused.empty or events.empty:
        return y
    ts, cell, sid = fused["ts_utc"], fused["unit_h3"], fused["scenario_id"]
    for ev in events.itertuples():
        y |= ((ts >= ev.start) & (ts <= ev.end) & cell.isin(ev.hexes)
              & sid.isin(ev.scenarios)).to_numpy()
    return y


def calibration(fused: pd.DataFrame, events: pd.DataFrame, bins: int = 10,
                scenario_id: str | None = None) -> dict:
    f = fused[fused["eligible"].astype(bool) & fused["mean"].notna()]
    if scenario_id:
        f = f[f["scenario_id"] == scenario_id]
    if f.empty:
        return {"bins": [], "brier": None, "brier_climatology": None, "brier_skill": None,
                "n": 0, "positives": 0}
    p = f["mean"].to_numpy(float)
    y = event_labels(f, events).astype(float)
    edges = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(p, edges[1:-1]), 0, bins - 1)
    out = []
    for b in range(bins):
        m = idx == b
        out.append({"bin": b, "lo": _r(edges[b], 2), "hi": _r(edges[b + 1], 2), "n": int(m.sum()),
                    "mean_predicted": _r(p[m].mean()) if m.any() else None,
                    "observed_frequency": _r(y[m].mean(), 4) if m.any() else None})
    brier = float(np.mean((p - y) ** 2))
    base = float(y.mean())
    clim = float(np.mean((base - y) ** 2))
    return {"bins": out, "brier": _r(brier, 4), "brier_climatology": _r(clim, 4),
            "brier_skill": _r(1 - brier / clim, 3) if clim > 0 else None,
            "n": int(len(p)), "positives": int(y.sum()), "base_rate": _r(base, 5)}


def coverage_matrix(alerts: pd.DataFrame, events: pd.DataFrame, scenarios, *,
                    lead_window_hours: float = 72, grace_hours: float = 12) -> pd.DataFrame:
    """scenario x event_type recall (NaN where there are no events of that type)."""
    ids = [s["id"] if isinstance(s, dict) else s for s in scenarios]
    types = list(COVERAGE_TYPES) + [t for t in EVENT_TYPES if t not in COVERAGE_TYPES
                                    and t in set(events.get("event_type", []))]
    mat = pd.DataFrame(np.nan, index=ids, columns=types)
    for t in types:
        evs = events[events["event_type"] == t] if len(events) else events
        if evs.empty:
            continue
        for sid in ids:
            n = sum(len(hits(alerts, ev, [sid], lead_window_hours, grace_hours)) > 0
                    for ev in evs.itertuples())
            mat.loc[sid, t] = n / len(evs)
    return mat


def _alert_block(a: pd.DataFrame, weeks: float) -> dict:
    n = len(a)
    m = int(a["matched"].sum()) if n else 0
    return {"alerts": n, "matched_alerts": m, "false_alarms": n - m,
            "ppv": _r(m / n) if n else None,
            "false_alarms_per_week": _r((n - m) / weeks, 2) if weeks > 0 else None}


def _event_block(e: pd.DataFrame) -> dict:
    n = len(e)
    d = int(e["detected"].sum()) if n else 0
    lead = e.loc[e["detected"], "lead_time_hours"].astype(float) if n else pd.Series(dtype=float)
    return {"events": n, "detected": d, "sensitivity": _r(d / n) if n else None,
            "mean_lead_time_hours": _r(lead.mean(), 1) if len(lead) else None,
            "median_lead_time_hours": _r(lead.median(), 1) if len(lead) else None,
            "detected_before_start": int((lead > 0).sum())}


TIERS = ("watch", "advisory", "warning")


def tier_alerts(alerts: pd.DataFrame, tier: str) -> pd.DataFrame:
    """Alerts that reached ``tier``, re-timed to when they first reached it."""
    col = f"first_seen_{tier}"
    if col not in alerts:              # older result files: approximate by severity_max
        rank = {t: i for i, t in enumerate(TIERS)}
        a = alerts[alerts["severity_max"].map(rank) >= rank[tier]].copy()
        a["timing"] = "approx_first_seen"
        return a
    a = alerts[alerts[col].notna()].copy()
    a["first_seen"] = pd.to_datetime(a[col], utc=True)
    return a


def chance_baseline(alerts: pd.DataFrame, events: pd.DataFrame, start: pd.Timestamp,
                    end: pd.Timestamp, *, n: int = 200, seed: int = 0, lead_window_hours: float = 72,
                    grace_hours: float = 12, mode: str = "uniform") -> dict:
    if alerts.empty or events.empty:
        return {"n": 0}
    rng = np.random.default_rng(seed)
    span = (end - start) / pd.Timedelta(hours=1)
    base = ((alerts["first_seen"] - start) / pd.Timedelta(hours=1)).to_numpy()
    keys = alerts["scenario_id"] + "|" + alerts["h3_cell"]
    codes, uniq = pd.factorize(keys)
    sens, ppv = [], []
    for _ in range(n):
        if mode == "seasonal":
            off = (rng.uniform(7 * 24, 45 * 24, size=len(uniq)) * rng.choice([-1, 1], size=len(uniq)))[codes]
        else:
            off = rng.uniform(30 * 24, span - 30 * 24, size=len(uniq))[codes]
        shifted = alerts.assign(first_seen=start + pd.to_timedelta((base + off) % span, unit="h"))
        a, e = match(shifted, events, lead_window_hours=lead_window_hours, grace_hours=grace_hours)
        sens.append(e["detected"].mean())
        ppv.append(a["matched"].mean())
    sens, ppv = np.array(sens), np.array(ppv)
    return {"n": n, "mode": mode, "sensitivity_mean": _r(sens.mean()),
            "sensitivity_p95": _r(np.quantile(sens, 0.95)),
            "ppv_mean": _r(ppv.mean()), "ppv_p95": _r(np.quantile(ppv, 0.95))}


def tier_blocks(alerts: pd.DataFrame, events: pd.DataFrame, weeks: float, start, end, *,
                lead_window_hours: float = 72, grace_hours: float = 12, chance_n: int = 200) -> dict:
    out = {}
    base_events = events.drop(columns=[c for c in ("detected", "lead_time_hours", "first_alert_time",
                                                    "first_alert_id", "alert_severity_at_first_hit",
                                                    "peak_severity_during_event", "n_alerts", "alert_ids")
                                       if c in events.columns])
    for tier in TIERS:
        ta = tier_alerts(alerts.drop(columns=[c for c in ("matched", "matched_event_ids") if c in alerts]), tier)
        a, e = match(ta, base_events, lead_window_hours=lead_window_hours, grace_hours=grace_hours)
        block = {**_event_block(e), **_alert_block(a, weeks)}
        if start is not None and chance_n:
            for mode in ("uniform", "seasonal"):
                ch = chance_baseline(ta, base_events, start, end, n=chance_n, mode=mode,
                                     lead_window_hours=lead_window_hours, grace_hours=grace_hours)
                block[f"chance_{mode}"] = ch
            ch = block["chance_seasonal"]
            if ch.get("sensitivity_mean") is not None and block["sensitivity"] is not None:
                block["sensitivity_lift"] = _r(block["sensitivity"] - ch["sensitivity_mean"])
            if ch.get("ppv_mean") and block["ppv"] is not None:
                block["ppv_lift"] = _r(block["ppv"] / ch["ppv_mean"], 2)
        if "timing" in ta:
            block["timing"] = "approximate (result file predates per-tier issue times)"
        out[tier] = block
    return out


def produce_scorecard(replay_df, events: pd.DataFrame | None = None, *, fused: pd.DataFrame | None = None,
                      scenarios=None, weeks: float | None = None, bins: int = 10,
                      lead_window_hours: float = 72, grace_hours: float = 12, chance_n: int = 200,
                      start=None, end=None) -> dict:
    """Everything the dashboard shows. ``replay_df`` is a ReplayResult or the matched
    alerts frame (then pass events, fused, weeks)."""
    if hasattr(replay_df, "alerts"):
        r = replay_df
        alerts, events, fused, weeks = r.alerts, r.events, r.fused, r.weeks
        start, end = r.start, r.end
        window = {"start": r.start.isoformat(), "end": r.end.isoformat(), "weeks": _r(weeks, 1)}
    else:
        alerts = replay_df
        window = {"weeks": _r(weeks, 1)}
    scen_ids = [s["id"] if isinstance(s, dict) else s for s in (scenarios or sorted(set(alerts["scenario_id"])))]
    overall = {**_event_block(events), **_alert_block(alerts, weeks)}
    cal = calibration(fused, events, bins) if fused is not None else None
    if cal:
        overall.update(brier=cal["brier"], brier_climatology=cal["brier_climatology"],
                       brier_skill=cal["brier_skill"])
    per_scenario = {}
    for sid in scen_ids:
        ev = events[events["scenarios"].map(lambda s: sid in s)] if len(events) else events
        rows = []
        for e in ev.itertuples():       # detected FOR THIS SCENARIO (an event may expect two)
            h = hits(alerts, e, [sid], lead_window_hours, grace_hours)
            rows.append({"detected": len(h) > 0, "lead_time_hours":
                         round((e.start - h["first_seen"].min()) / pd.Timedelta(hours=1), 2) if len(h) else None})
        sub = pd.DataFrame(rows, columns=["detected", "lead_time_hours"])
        block = {**(_event_block(sub) if len(sub) else {"events": 0, "detected": 0, "sensitivity": None}),
                 **_alert_block(alerts[alerts["scenario_id"] == sid], weeks)}
        if fused is not None:
            c = calibration(fused, events, bins, scenario_id=sid)
            block.update(brier=c["brier"], brier_skill=c["brier_skill"])
        per_scenario[sid] = block
    by_type = {t: _event_block(events[events["event_type"] == t])
               for t in sorted(set(events["event_type"]))} if len(events) else {}
    cov = coverage_matrix(alerts, events, scen_ids, lead_window_hours=lead_window_hours,
                          grace_hours=grace_hours)
    tiers = tier_blocks(alerts, events, weeks, start, end, lead_window_hours=lead_window_hours,
                        grace_hours=grace_hours, chance_n=chance_n)
    for k in ("chance_uniform", "chance_seasonal", "sensitivity_lift", "ppv_lift"):
        overall[k] = tiers["watch"].get(k)
    return {
        "window": window,
        "overall": overall,
        "by_tier": tiers,
        "per_scenario": per_scenario,
        "by_event_type": by_type,
        "coverage_matrix": {"scenarios": list(cov.index), "event_types": list(cov.columns),
                            "values": [[_r(v) for v in row] for row in cov.to_numpy()],
                            "n_events": {t: int((events["event_type"] == t).sum()) if len(events) else 0
                                         for t in cov.columns}},
        "calibration": cal,
        "matching": {"lead_window_hours": lead_window_hours, "grace_hours": grace_hours},
    }
