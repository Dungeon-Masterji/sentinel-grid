"""Backtest CLI.

    python -m packages.pipelines.backtest.run --start 2023-01-01 --end 2024-12-31 \\
        --out data/processed/backtest/results.parquet

Steps: pull_history (idempotent monthly files, incl. 90 days of warm-up before --start)
-> replay (15-min ticks, zero lookahead) -> match to configs/ground_truth_events.yaml ->
metrics. Writes next to --out:

    results.parquet        one row per alert, matched / matched_event_ids
    events.json            ground-truth events with detection status and lead times
    scorecard.json         headline + per-scenario metrics, coverage matrix, calibration
    calibration.json       calibration bins (overall and per scenario)
    event_traces.parquet   hour-by-hour signal + fused-score traces around every event
    fused_hourly.parquet   every scenario x hex x hour fused score (local; not published)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from packages.pipelines.backtest.events import EVENTS_YAML, matching_params  # noqa: E402
from packages.pipelines.backtest.history import BACKTEST_DIR, load_history, pull_history  # noqa: E402
from packages.pipelines.backtest.metrics import calibration, produce_scorecard  # noqa: E402
from packages.pipelines.backtest.replay import replay  # noqa: E402
from packages.pipelines.fusion.config import SCENARIOS_YAML, load_scenarios  # noqa: E402

log = logging.getLogger("sentinel.backtest.run")
TRACE_BEFORE = pd.Timedelta(hours=96)
TRACE_AFTER = pd.Timedelta(hours=36)


def _iso(t):
    return None if t is None or (isinstance(t, float) and t != t) else pd.Timestamp(t).isoformat()


def event_traces(panel: pd.DataFrame, units: pd.DataFrame, fused: pd.DataFrame, events: pd.DataFrame,
                 cfg: dict) -> pd.DataFrame:
    """Long frame: event_id, ts_utc, series, value. Series are each signal of the
    event's expected scenarios (mean over the event's hexes, read from each pillar's
    source hex) and 'fused:<scenario>' (max over the event's hexes) for EVERY scenario."""
    scen = {s["id"]: s for s in cfg["scenarios"]}
    pillar = {k: m["pillar"] for k, m in cfg["signals"].items()}
    u = units.set_index("h3")
    out = []
    for ev in events.itertuples():
        lo, hi = ev.start - TRACE_BEFORE, ev.end + TRACE_AFTER
        cells = [c for c in ev.hexes if c in u.index]
        sigs = sorted({s["name"] for sid in ev.scenarios for s in scen[sid]["signals"]})
        for sig in sigs:
            col = {"env": "env_h3", "occ": None, "clin": "clinical_h3"}[pillar[sig]]
            src = set(cells) if col is None else set(u.loc[cells, col])
            p = panel[(panel["signal"] == sig) & panel["h3"].isin(src)
                      & (panel["ts_utc"] >= lo) & (panel["ts_utc"] <= hi) & ~panel["suppressed"]]
            s = p.groupby("ts_utc")["value"].mean()
            out.append(pd.DataFrame({"event_id": ev.id, "ts_utc": s.index, "series": sig,
                                     "value": s.to_numpy()}))
        f = fused[fused["unit_h3"].isin(cells) & (fused["ts_utc"] >= lo) & (fused["ts_utc"] <= hi)]
        for sid, g in f.groupby("scenario_id"):
            s = g.groupby("ts_utc")["mean"].max()
            out.append(pd.DataFrame({"event_id": ev.id, "ts_utc": s.index, "series": f"fused:{sid}",
                                     "value": s.to_numpy()}))
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame(
        columns=["event_id", "ts_utc", "series", "value"])


def _clean(o):
    """NaN / inf -> None, recursively (JSON has no NaN)."""
    if isinstance(o, float):
        return o if np.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def _events_json(events: pd.DataFrame, alerts: pd.DataFrame, cfg: dict, lead_h: float,
                 grace_h: float) -> list[dict]:
    from packages.pipelines.backtest.events import hits
    out = []
    for ev in events.itertuples():
        # every alert (any scenario) in the event's space-time window, for the timeline
        h = hits(alerts, ev, [s["id"] for s in cfg["scenarios"]], lead_h, grace_h)
        out.append({
            "id": ev.id, "label": ev.label, "event_type": ev.event_type,
            "start": _iso(ev.start), "end": _iso(ev.end), "peak": _iso(ev.peak),
            "affected_emirates": ev.affected_emirates, "n_hexes": len(ev.hexes),
            "expected_scenarios": ev.scenarios, "severity_truth": ev.severity_truth,
            "peak_value": ev.peak_value, "source_url": ev.source_url, "notes": ev.notes,
            "detected": bool(ev.detected), "lead_time_hours": ev.lead_time_hours,
            "first_alert_time": _iso(ev.first_alert_time),
            "alert_severity_at_first_hit": ev.alert_severity_at_first_hit,
            "peak_severity_during_event": ev.peak_severity_during_event,
            "n_matching_alerts": int(ev.n_alerts),
            "alerts": [{"id": a.id, "scenario_id": a.scenario_id, "h3_cell": a.h3_cell,
                        "first_seen": _iso(a.first_seen), "severity_first": a.severity_first,
                        "severity_max": a.severity_max, "score_max": round(float(a.score_max), 4),
                        "expected": a.scenario_id in ev.scenarios}
                       for a in h.sort_values("first_seen").itertuples()],
        })
    return out


def _load_previous(out: Path, start, end, events_yaml, units):
    from packages.pipelines.backtest.events import load_events, match
    from packages.pipelines.backtest.replay import ReplayResult
    out = Path(out)
    alerts = pd.read_parquet(out)
    alerts = alerts.drop(columns=[c for c in ("matched", "matched_event_ids") if c in alerts])
    fused = pd.read_parquet(out.parent / "fused_hourly.parquet")
    s, e = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
    events = load_events(events_yaml, units)
    events = events[(events["end"] >= s) & (events["start"] < e)].reset_index(drop=True)
    mp_ = matching_params(events_yaml)
    alerts, events = match(alerts, events, lead_window_hours=mp_["lead_window_hours"],
                           grace_hours=mp_["grace_hours"])
    prev = json.loads((out.parent / "scorecard.json").read_text()) if (out.parent / "scorecard.json").is_file() else {}
    return ReplayResult(alerts, events, fused, s, e, {**prev.get("timings", {}), "rescored": True})


def run(start: date, end: date, out: Path, *, scenarios_yaml=SCENARIOS_YAML,
        events_yaml=EVENTS_YAML, history_dir: Path = BACKTEST_DIR, workers: int | None = None,
        skip_history: bool = False, rescore: bool = False) -> dict:
    """Full run, or with ``rescore`` recompute every metric and output from the alerts +
    hourly scores a previous run saved next to ``out`` (no replay; e.g. after changing the
    matching rules or the ground-truth file)."""
    cfg = load_scenarios(scenarios_yaml)
    hist_days = int(cfg["spatial"].get("history_days", 90))
    warm = pd.Timestamp(start) - pd.Timedelta(days=hist_days + 3)
    end_excl = pd.Timestamp(end) + pd.Timedelta(days=1)
    t0 = time.time()
    if not skip_history:
        pull_history(warm.date(), end, scenarios_yaml=scenarios_yaml, out_dir=history_dir)
    t_hist = time.time() - t0
    panel, units = load_history(warm, end, history_dir)
    if rescore:
        r = _load_previous(out, start, end_excl, events_yaml, units)
    else:
        r = replay(start, end_excl, scenarios_yaml, events_yaml, panel=panel, units=units,
                   workers=workers)
    mp_ = matching_params(events_yaml)
    card = produce_scorecard(r, scenarios=cfg["scenarios"], bins=mp_["calibration_bins"],
                             lead_window_hours=mp_["lead_window_hours"], grace_hours=mp_["grace_hours"])
    card["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    card["timings"] = {**r.timings, "history_s": round(t_hist, 1), "total_s": round(time.time() - t0, 1)}
    card["notice"] = ("Environmental signals are real (ERA5 + CAMS). Clinical (Synthea) and "
                      "occupational (simulated workforce) signals are synthetic, driven by the real "
                      "weather. See docs/backtest_methodology.md.")
    card["sources"] = {k: int(v) for k, v in panel["source"].value_counts().items()}

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    a = r.alerts.copy()
    a["matched_event_ids"] = a["matched_event_ids"].map(json.dumps)
    a.to_parquet(out, compression="zstd")
    r.fused.to_parquet(out.parent / "fused_hourly.parquet", compression="zstd")
    (out.parent / "events.json").write_text(json.dumps(_clean(
        _events_json(r.events, r.alerts, cfg, mp_["lead_window_hours"], mp_["grace_hours"])),
        indent=2, default=str, allow_nan=False))
    cal = {"overall": card["calibration"],
           "per_scenario": {s["id"]: calibration(r.fused, r.events, mp_["calibration_bins"], s["id"])
                            for s in cfg["scenarios"]}}
    (out.parent / "calibration.json").write_text(json.dumps(_clean(cal), indent=2, default=str, allow_nan=False))
    (out.parent / "scorecard.json").write_text(json.dumps(_clean(card), indent=2, default=str, allow_nan=False))
    tr = event_traces(panel, units, r.fused, r.events, cfg)
    tr.to_parquet(out.parent / "event_traces.parquet", compression="zstd")
    return card


def _fmt(card: dict) -> str:
    o = card["overall"]
    lines = [f"window {card['window']}",
             f"sensitivity {o['sensitivity']} ({o['detected']}/{o['events']} events) · PPV {o['ppv']} "
             f"({o['matched_alerts']}/{o['alerts']} alerts) · false alarms/week {o['false_alarms_per_week']}",
             f"mean lead {o['mean_lead_time_hours']} h (median {o['median_lead_time_hours']} h) · Brier "
             f"{o.get('brier')} vs climatology {o.get('brier_climatology')} (skill {o.get('brier_skill')})"]
    for tier, b in card.get("by_tier", {}).items():
        c = b.get("chance_seasonal") or {}
        lines.append(f"  {tier:>8}+: alerts {b['alerts']:>6} · sens {b['sensitivity']} (chance {c.get('sensitivity_mean')}) "
                     f"· PPV {b['ppv']} (chance {c.get('ppv_mean')}, p95 {c.get('ppv_p95')}, lift {b.get('ppv_lift')}x) "
                     f"· FA/wk {b['false_alarms_per_week']} · median lead {b['median_lead_time_hours']} h")
    for sid, b in card["per_scenario"].items():
        lines.append(f"  {sid:26} events {b.get('detected', 0)}/{b.get('events', 0)} · alerts {b['alerts']} "
                     f"· PPV {b['ppv']} · FA/wk {b['false_alarms_per_week']}")
    cm = card["coverage_matrix"]
    lines.append("coverage (recall) " + " | ".join(f"{t} (n={cm['n_events'][t]})" for t in cm["event_types"]))
    for sid, row in zip(cm["scenarios"], cm["values"]):
        lines.append(f"  {sid:26} " + "  ".join("  -  " if v is None else f"{v:5.2f}" for v in row))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", type=date.fromisoformat, default=date(2023, 1, 1))
    ap.add_argument("--end", type=date.fromisoformat, default=date(2024, 12, 31))
    ap.add_argument("--out", type=Path, default=BACKTEST_DIR / "results" / "results.parquet")
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--skip-history", action="store_true", help="use existing monthly files only")
    ap.add_argument("--rescore", action="store_true",
                    help="recompute metrics from the alerts/fused files a previous run wrote next to --out")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    for n in ("cmdstanpy", "prophet", "sentinel.fusion.detect", "sentinel.fusion.baselines"):
        logging.getLogger(n).setLevel(logging.ERROR)
    card = run(args.start, args.end, args.out, workers=args.workers,
               skip_history=args.skip_history or args.rescore, rescore=args.rescore)
    print(_fmt(card))
    print(f"\nwrote {args.out.parent}/ ({card['timings']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
