"""Ground-truth events (configs/ground_truth_events.yaml) and alert <-> event matching.

An alert MATCHES an event when all three hold:
  scenario  the alert's scenario is in scenario_map[event_type]
  space     the alert's hex is one of the event's hexes ("auto" = every site hex in the
            event's affected_emirates)
  time      the alert was first issued (first_seen, i.e. the first 15-min replay tick it
            appeared at) within [start - lead_window_hours, end + grace_hours]

A dust storm overlapping a heatwave is just two events: each is scored on its own and
one alert can match both if its scenario maps to both types.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
EVENTS_YAML = REPO_ROOT / "configs" / "ground_truth_events.yaml"
EVENT_TYPES = ("heatwave", "dust_storm", "flood", "ramadan_stress", "infectious_cluster",
               "pollution_episode", "heat_wave_combined_with_dust")
REQUIRED = ("id", "label", "event_type", "affected_emirates", "severity_truth", "source_url")


def load_events_config(path: Path | str | dict = EVENTS_YAML) -> dict:
    cfg = path if isinstance(path, dict) else yaml.safe_load(Path(path).read_text())
    problems = []
    seen = set()
    for e in cfg.get("events") or []:
        for k in REQUIRED:
            if not e.get(k):
                problems.append(f"{e.get('id')}: missing {k}")
        if not (e.get("start_time") or e.get("start_date")):
            problems.append(f"{e.get('id')}: missing start_time")
        if e.get("event_type") not in EVENT_TYPES:
            problems.append(f"{e.get('id')}: unknown event_type {e.get('event_type')}")
        if e.get("event_type") not in (cfg.get("scenario_map") or {}):
            problems.append(f"{e.get('id')}: event_type not in scenario_map")
        if e.get("id") in seen:
            problems.append(f"duplicate id {e.get('id')}")
        seen.add(e.get("id"))
    if problems:
        raise ValueError("ground_truth_events.yaml: " + "; ".join(problems))
    return cfg


def _ts(v, end_of_day: bool = False) -> pd.Timestamp:
    t = pd.Timestamp(v)
    if t.tzinfo is None:                       # a bare date: Asia/Dubai day
        t = t.tz_localize("Asia/Dubai")
        if end_of_day:
            t = t + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    return t.tz_convert("UTC")


def load_events(path: Path | str | dict = EVENTS_YAML, units: pd.DataFrame | None = None) -> pd.DataFrame:
    """One row per event: id, label, event_type, start, end, peak (UTC), hexes,
    scenarios (expected), severity_truth, source_url, notes, ..."""
    cfg = load_events_config(path)
    smap = cfg.get("scenario_map") or {}
    rows = []
    for e in cfg["events"]:
        start = _ts(e.get("start_time") or e["start_date"])
        end = _ts(e.get("end_time") or e.get("end_date") or e.get("start_date"), end_of_day=True)
        peak = _ts(e["peak_time"]) if e.get("peak_time") else (
            _ts(e["peak_date"]) if e.get("peak_date") else start)
        hexes = e.get("affected_hexes", "auto")
        if hexes == "auto" or hexes is None:
            if units is None:
                hexes = []
            else:
                em = set(e["affected_emirates"])
                hexes = [u.h3 for u in units.itertuples() if em & set(list(u.emirates))]
        rows.append({"id": e["id"], "label": e["label"], "event_type": e["event_type"],
                     "start": start, "end": end, "peak": peak, "hexes": list(hexes),
                     "affected_emirates": list(e["affected_emirates"]),
                     "scenarios": list(smap.get(e["event_type"], [])),
                     "severity_truth": e["severity_truth"], "peak_value": e.get("peak_value"),
                     "source_url": e["source_url"], "notes": (e.get("notes") or "").strip()})
    return pd.DataFrame(rows)


def matching_params(path: Path | str | dict = EVENTS_YAML) -> dict:
    m = (load_events_config(path).get("matching") or {})
    return {"lead_window_hours": float(m.get("lead_window_hours", 72)),
            "grace_hours": float(m.get("grace_hours", 12)),
            "calibration_bins": int(m.get("calibration_bins", 10))}


def hits(alerts: pd.DataFrame, ev, scenarios, lead_h: float, grace_h: float) -> pd.DataFrame:
    """Alerts of ``scenarios`` that match event ``ev`` in space and time."""
    if alerts.empty:
        return alerts
    lo = ev.start - pd.Timedelta(hours=lead_h)
    hi = ev.end + pd.Timedelta(hours=grace_h)
    m = (alerts["scenario_id"].isin(list(scenarios)) & alerts["h3_cell"].isin(ev.hexes)
         & (alerts["first_seen"] >= lo) & (alerts["first_seen"] <= hi))
    return alerts[m]


def match(alerts: pd.DataFrame, events: pd.DataFrame, *, lead_window_hours: float = 72,
          grace_hours: float = 12) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(alerts + matched_event_ids/matched, events + detection columns)."""
    alerts = alerts.copy()
    alerts["matched_event_ids"] = [[] for _ in range(len(alerts))]
    ev_out = []
    for ev in events.itertuples():
        h = hits(alerts, ev, ev.scenarios, lead_window_hours, grace_hours)
        for i in h.index:
            alerts.at[i, "matched_event_ids"].append(ev.id)
        rec = ev._asdict()
        rec.pop("Index", None)
        if len(h):
            first = h.sort_values("first_seen").iloc[0]
            rank = {"watch": 0, "advisory": 1, "warning": 2}
            rec.update(detected=True, first_alert_time=first["first_seen"],
                       first_alert_id=first["id"],
                       lead_time_hours=round((ev.start - first["first_seen"]) / pd.Timedelta(hours=1), 2),
                       alert_severity_at_first_hit=first["severity_first"],
                       peak_severity_during_event=max(h["severity_max"], key=lambda s: rank[s]),
                       n_alerts=int(len(h)), alert_ids=list(h["id"]))
        else:
            rec.update(detected=False, first_alert_time=None, first_alert_id=None,
                       lead_time_hours=None, alert_severity_at_first_hit=None,
                       peak_severity_during_event=None, n_alerts=0, alert_ids=[])
        ev_out.append(rec)
    alerts["matched"] = alerts["matched_event_ids"].map(bool)
    return alerts, pd.DataFrame(ev_out)
