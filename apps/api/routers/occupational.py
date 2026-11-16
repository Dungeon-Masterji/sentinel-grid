"""Occupational signal pillar: synthetic workforce at 15 real UAE sites.

Synthetic workforce, demographically modelled on public aggregates. Not real
workers, and not for individual employment decisions (docs/ETHICS.md).

Reads three small parquet files produced by
packages/pipelines/synth/occupational_run.py:

    sites.parquet, daily_site_rollup.parquet, features.parquet

looked up in $OCCUPATIONAL_DATA_DIR, ./data/occupational (Azure, copied in by
.github/workflows/main_sentinel-grid-api.yml), then the repo's data/processed
layout (local). Only pandas and pyarrow are needed.

Time: data is stored in UTC. Every timestamp and date in a response is
Asia/Dubai local time (ISO-8601 with +04:00). Rollup dates are Asia/Dubai rota days.

Risk tier per site for the current rota day:
    red    (elevated) absence_rate > 5%  OR  mean apparent temperature today > 42 °C
    amber  (watch)    absence_rate > 3.5% OR max apparent temperature today >= 40 °C
                      OR mean PM2.5 today > 55 µg/m³
    green  otherwise
risk_score = max(absence_rate / 0.05, mean_apparent_temp / 42); >= 1 means elevated.
"""
from __future__ import annotations

import math
import os
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query

router = APIRouter()

API_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = API_DIR.parents[1] if API_DIR.name == "api" else None
DUBAI = ZoneInfo("Asia/Dubai")
NOTICE = ("Synthetic workforce — demographically modelled on public aggregates; not real "
          "workers. Not for individual employment decisions.")
MAX_RANGE_DAYS = 366

ELEVATED_ABSENCE = 0.05
ELEVATED_MEAN_AT = 42.0
WATCH_ABSENCE = 0.035
WATCH_MAX_AT = 40.0
WATCH_PM25 = 55.0


def _locations() -> list[dict[str, Path]]:
    env = os.environ.get("OCCUPATIONAL_DATA_DIR")
    locs = []
    if env:
        d = Path(env)
        locs.append({"sites": d / "sites.parquet", "rollup": d / "daily_site_rollup.parquet",
                     "features": d / "features.parquet"})
    d = API_DIR / "data" / "occupational"
    locs.append({"sites": d / "sites.parquet", "rollup": d / "daily_site_rollup.parquet",
                 "features": d / "features.parquet"})
    if REPO_ROOT is not None:
        p = REPO_ROOT / "data" / "processed"
        locs.append({"sites": p / "occupational" / "sites.parquet",
                     "rollup": p / "occupational" / "daily_site_rollup.parquet",
                     "features": p / "features" / "occupational.parquet"})
    return locs


class _Store:
    """Lazy, mtime-aware cache of the three parquet files."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._key: tuple | None = None
        self.sites = self.rollup = self.features = None
        self.generated_at: str | None = None

    def load(self):
        loc = next((l for l in _locations() if all(p.is_file() for p in l.values())), None)
        if loc is None:
            raise HTTPException(503, detail={
                "error": "occupational data not available yet",
                "hint": "run python -m packages.pipelines.synth.occupational_run"})
        key = tuple((str(p), p.stat().st_mtime) for p in loc.values())
        with self._lock:
            if key != self._key:
                import pandas as pd
                import pyarrow.parquet as pq
                self.sites = pd.read_parquet(loc["sites"])
                r = pd.read_parquet(loc["rollup"])
                r["date"] = pd.to_datetime(r["date"]).dt.date
                self.rollup = r
                f = pd.read_parquet(loc["features"])
                f["ts_utc"] = pd.to_datetime(f["ts_utc"], utc=True)
                f["local_day"] = f["ts_utc"].dt.tz_convert(DUBAI).dt.date
                self.features = f
                md = pq.read_schema(loc["rollup"]).metadata or {}
                self.generated_at = (md.get(b"generated_at") or b"").decode() or None
                self._key = key
        return self.sites, self.rollup, self.features


store = _Store()


def _num(x, nd: int = 4):
    """JSON-safe float (NaN/NA -> None)."""
    try:
        if x is None or (isinstance(x, float) and math.isnan(x)):
            return None
        f = float(x)
        return None if math.isnan(f) else round(f, nd)
    except (TypeError, ValueError):
        return None


def _int(x):
    try:
        return None if x is None or (isinstance(x, float) and math.isnan(x)) else int(x)
    except (TypeError, ValueError):
        return None


def _local_iso(ts) -> str:
    return ts.tz_convert(DUBAI).isoformat()


def _now_utc():
    import pandas as pd
    return pd.Timestamp(datetime.now(timezone.utc))


def _envelope(**payload) -> dict:
    return {"synthetic": True, "notice": NOTICE, "timezone": "Asia/Dubai",
            "generated_at": store.generated_at, **payload}


def classify(absence_rate, mean_at, max_at, mean_pm25) -> tuple[str, float]:
    a = absence_rate if absence_rate is not None else 0.0
    m = mean_at if mean_at is not None else 0.0
    score = max(a / ELEVATED_ABSENCE, m / ELEVATED_MEAN_AT)
    if a > ELEVATED_ABSENCE or m > ELEVATED_MEAN_AT:
        return "red", score
    if (a > WATCH_ABSENCE or (max_at is not None and max_at >= WATCH_MAX_AT)
            or (mean_pm25 is not None and mean_pm25 > WATCH_PM25)):
        return "amber", score
    return "green", score


def _current_day(rollup) -> date:
    """Today in Asia/Dubai, capped to the latest rota day with data."""
    today = datetime.now(DUBAI).date()
    days = rollup["date"]
    upto = days[days <= today]
    return upto.max() if len(upto) else days.max()


def _site_status(site, rollup, features, day: date, now_utc) -> dict:
    sid = site["site_id"]
    r = rollup[(rollup["site_id"] == sid) & (rollup["date"] == day)]
    r = r.iloc[0] if len(r) else None
    f_day = features[(features["site_id"] == sid) & (features["local_day"] == day)]
    mean_at = _num(f_day["apparent_temp"].mean(), 1) if len(f_day) else None
    max_at = _num(f_day["apparent_temp"].max(), 1) if len(f_day) else None
    pm25 = _num(f_day["pm25"].mean(), 1) if len(f_day) else None
    f_now = features[(features["site_id"] == sid) & (features["ts_utc"] <= now_utc)]
    cur = f_now.iloc[-1] if len(f_now) else None
    absence = _num(r["absence_rate"]) if r is not None else None
    tier, score = classify(absence, mean_at, max_at, pm25)

    week = rollup[(rollup["site_id"] == sid) & (rollup["date"] <= day)
                  & (rollup["date"] > day - timedelta(days=7))].sort_values("date")
    return {
        "worker_count": _int(site["worker_count"]),
        "scheduled_today": _int(r["scheduled_count"]) if r is not None else None,
        "worked_today": _int(r["worked_count"]) if r is not None else None,
        "absence_rate": absence,
        "heat_absence_rate": _num(r["heat_absence_rate"]) if r is not None else None,
        "respiratory_absence_rate": _num(r["respiratory_absence_rate"]) if r is not None else None,
        "mean_ppe_compliance": _num(r["mean_ppe_compliance"]) if r is not None else None,
        "mean_apparent_temp_today": mean_at,
        "max_apparent_temp_today": max_at,
        "mean_pm25_today": pm25,
        "workers_on_site_now": _int(cur["workers_on_site"]) if cur is not None else None,
        "shift_now": (str(cur["shift_type"]) if cur is not None else None),
        "apparent_temp_now": _num(cur["apparent_temp"], 1) if cur is not None else None,
        "observed_at": _local_iso(cur["ts_utc"]) if cur is not None else None,
        "risk_tier": tier,
        "risk_score": round(score, 3),
        "worked_hours_7d": [{"date": d.isoformat(), "worked_hours": _num(w, 1)}
                            for d, w in zip(week["date"], week["worked_hours"])],
    }


def _site_meta(site) -> dict:
    return {"site_id": site["site_id"], "name": site["name"], "operator": site["operator"],
            "sector": site["sector"], "emirate": site["emirate"],
            "lat": _num(site["lat"], 6), "lon": _num(site["lon"], 6),
            "h3_res8": site["h3_res8"], "shift_pattern": site["shift_pattern"],
            "heat_exposure": site["heat_exposure"]}


@router.get("/sites")
def list_sites():
    """All sites with current status (worker count, current risk tier)."""
    sites, rollup, features = store.load()
    day = _current_day(rollup)
    now = _now_utc()
    items = []
    for _, s in sites.iterrows():
        items.append({**_site_meta(s), **_site_status(s, rollup, features, day, now)})
    worked = [i["worked_today"] or 0 for i in items]
    return _envelope(
        as_of=datetime.now(DUBAI).isoformat(timespec="seconds"),
        current_day=day.isoformat(),
        totals={
            "sites": len(items),
            "roster": int(sum(i["worker_count"] or 0 for i in items)),
            "active_today": int(sum(worked)),
            "on_site_now": int(sum(i["workers_on_site_now"] or 0 for i in items)),
            "elevated_sites": sum(i["risk_tier"] == "red" for i in items),
            "watch_sites": sum(i["risk_tier"] == "amber" for i in items),
        },
        risk_definition={"elevated": "absence_rate > 5% OR mean apparent temp today > 42 °C",
                         "watch": "absence_rate > 3.5% OR max apparent temp today >= 40 °C "
                                  "OR mean PM2.5 today > 55 µg/m³"},
        sites=items)


@router.get("/sites/{site_id}")
def site_detail(site_id: str):
    """One site: metadata, current status, latest 24h hourly metrics, last 7 rota days."""
    sites, rollup, features = store.load()
    match = sites[sites["site_id"] == site_id]
    if match.empty:
        raise HTTPException(404, detail=f"unknown site_id {site_id!r}")
    s = match.iloc[0]
    day = _current_day(rollup)
    now = _now_utc()
    f = features[(features["site_id"] == site_id) & (features["ts_utc"] <= now)].tail(24)
    hourly = [{
        "ts": _local_iso(r.ts_utc),
        "apparent_temp": _num(r.apparent_temp, 1), "pm25": _num(r.pm25, 1),
        "pm10": _num(r.pm10, 1), "workers_on_site": _int(r.workers_on_site),
        "scheduled_on_site": _int(r.scheduled_on_site),
        "absence_rate_last_1h": _num(r.absence_rate_last_1h),
        "ppe_compliance": _num(r.ppe_compliance), "shift_type": r.shift_type,
    } for r in f.itertuples()]
    week = rollup[(rollup["site_id"] == site_id) & (rollup["date"] <= day)].tail(7)
    return _envelope(
        as_of=datetime.now(DUBAI).isoformat(timespec="seconds"), current_day=day.isoformat(),
        site={**_site_meta(s), **_site_status(s, rollup, features, day, now)},
        last_24h=hourly, last_7_days=_rollup_rows(week))


def _rollup_rows(df) -> list[dict]:
    return [{
        "site_id": r.site_id, "date": r.date.isoformat(),
        "scheduled_count": _int(r.scheduled_count), "worked_count": _int(r.worked_count),
        "absence_rate": _num(r.absence_rate), "heat_absence_rate": _num(r.heat_absence_rate),
        "respiratory_absence_rate": _num(r.respiratory_absence_rate),
        "mean_ppe_compliance": _num(r.mean_ppe_compliance),
        "peak_absence_hour": _int(r.peak_absence_hour) if r.peak_absence_hour is not None
        and str(r.peak_absence_hour) != "<NA>" else None,
        "scheduled_hours": _num(r.scheduled_hours, 1), "worked_hours": _num(r.worked_hours, 1),
    } for r in df.itertuples()]


@router.get("/rollup")
def rollup(since: date | None = Query(None, description="YYYY-MM-DD (Asia/Dubai), inclusive"),
           until: date | None = Query(None, description="YYYY-MM-DD (Asia/Dubai), inclusive"),
           site_id: str | None = Query(None)):
    """daily_site_rollup rows. Defaults to the 7 rota days ending on the current day."""
    sites, roll, _ = store.load()
    if site_id is not None and site_id not in set(sites["site_id"]):
        raise HTTPException(404, detail=f"unknown site_id {site_id!r}")
    until = until or _current_day(roll)
    since = since or until - timedelta(days=6)
    if since > until:
        raise HTTPException(422, detail="since must be on or before until")
    if (until - since).days >= MAX_RANGE_DAYS:
        raise HTTPException(422, detail=f"range limited to {MAX_RANGE_DAYS} days")
    df = roll[(roll["date"] >= since) & (roll["date"] <= until)]
    if site_id:
        df = df[df["site_id"] == site_id]
    df = df.sort_values(["date", "site_id"])
    return _envelope(since=since.isoformat(), until=until.isoformat(), site_id=site_id,
                     first_data_day=roll["date"].min().isoformat(),
                     latest_data_day=roll["date"].max().isoformat(),
                     rows=_rollup_rows(df))
