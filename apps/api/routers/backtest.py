"""Backtest results (packages/pipelines/backtest), read-only.

Files are produced by `python -m packages.pipelines.backtest.run` (weekly:
.github/workflows/backtest-weekly.yml), which force-pushes them to the orphan branch
`data/processed/backtest`:

    scorecard.json  events.json  calibration.json  event_traces.parquet

Where the API reads them from (first hit wins):
  1. $BACKTEST_DATA_DIR
  2. the state dir (Azure: /home/data/sentinel-grid/backtest), downloaded from the data
     branch on first use and again when older than BACKTEST_TTL_SECONDS (default 3600)
  3. ./data/backtest (copied in at deploy time)
  4. <repo>/data/processed/backtest/results (local development)

Times in responses are Asia/Dubai.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException

router = APIRouter()
log = logging.getLogger("sentinel.api.backtest")

API_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = API_DIR.parents[1] if API_DIR.name == "api" else None
DUBAI = ZoneInfo("Asia/Dubai")
FILES = ("scorecard.json", "events.json", "calibration.json", "event_traces.parquet")
DEFAULT_SOURCE = ("https://raw.githubusercontent.com/Dungeon-Masterji/sentinel-grid/"
                  "refs/heads/data/processed/backtest/data/processed/backtest/results")
NOTICE = ("Backtest of the live detector on 2023-2024 history. Environmental signals are real "
          "(ERA5 + CAMS); clinical and occupational signals are synthetic, driven by the real "
          "weather. Heat and dust results are a validation; infectious results are a "
          "plausibility check only. See docs/backtest_methodology.md.")


def _on_azure() -> bool:
    return bool(os.environ.get("WEBSITE_SITE_NAME"))


def _state_dir() -> Path:
    env = os.environ.get("BACKTEST_STATE_DIR")
    if env:
        return Path(env)
    if _on_azure():
        return Path("/home/data/sentinel-grid/backtest")
    return API_DIR / "data" / "backtest_state"


def _candidates() -> list[Path]:
    out = []
    if os.environ.get("BACKTEST_DATA_DIR"):
        out.append(Path(os.environ["BACKTEST_DATA_DIR"]))
    out += [_state_dir(), API_DIR / "data" / "backtest"]
    if REPO_ROOT is not None:
        out.append(REPO_ROOT / "data" / "processed" / "backtest" / "results")
    return out


def _finite(o):
    """JSON from older runs may contain NaN; responses must not."""
    if isinstance(o, float):
        return o if o == o and o not in (float("inf"), float("-inf")) else None
    if isinstance(o, dict):
        return {k: _finite(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_finite(v) for v in o]
    return o


class _Store:
    def __init__(self):
        self._lock = threading.Lock()
        self.last_pull = 0.0
        self.status = "never"
        self._key = None
        self.data: dict = {}

    def pull(self) -> None:
        url = os.environ.get("BACKTEST_SOURCE_URL") or (DEFAULT_SOURCE if _on_azure() else None)
        if not url:
            return
        if time.time() - self.last_pull < float(os.environ.get("BACKTEST_TTL_SECONDS", "3600")):
            return
        import requests
        self.last_pull = time.time()
        dest = _state_dir()
        dest.mkdir(parents=True, exist_ok=True)
        try:
            got = {}
            for name in FILES:
                r = requests.get(f"{url.rstrip('/')}/{name}", timeout=20)
                r.raise_for_status()
                got[name] = r.content
            for name, body in got.items():
                tmp = dest / f".{name}.tmp"
                tmp.write_bytes(body)
                os.replace(tmp, dest / name)
            self.status = "ok"
        except Exception as e:  # noqa: BLE001 - keep serving the last good copy
            self.status = f"failed: {e.__class__.__name__}: {e}"[:200]
            log.warning("backtest pull failed: %s", self.status)

    def load(self) -> dict:
        self.pull()
        d = next((p for p in _candidates() if (p / "scorecard.json").is_file()), None)
        if d is None:
            raise HTTPException(503, detail={"error": "backtest results not available yet",
                                              "hint": "run python -m packages.pipelines.backtest.run"})
        key = tuple((n, (d / n).stat().st_mtime) for n in FILES if (d / n).is_file())
        with self._lock:
            if key != self._key:
                data = {n.split(".")[0]: _finite(json.loads((d / n).read_text()))
                        for n in ("scorecard.json", "events.json", "calibration.json")
                        if (d / n).is_file()}
                data["traces_path"] = d / "event_traces.parquet"
                data["traces"] = None
                self.data, self._key = data, key
        return self.data

    def traces(self):
        data = self.load()
        if data.get("traces") is None and data["traces_path"].is_file():
            import pandas as pd
            data["traces"] = pd.read_parquet(data["traces_path"])
        return data.get("traces")


store = _Store()


def _local(ts):
    if ts is None:
        return None
    import pandas as pd
    t = pd.Timestamp(ts)
    t = t.tz_localize("UTC") if t.tzinfo is None else t
    return t.tz_convert(DUBAI).isoformat()


def _event_out(e: dict, with_alerts: bool = True) -> dict:
    out = {k: v for k, v in e.items() if k != "alerts"}
    for k in ("start", "end", "peak", "first_alert_time"):
        out[k] = _local(e.get(k))
    if with_alerts:
        out["alerts"] = [{**a, "first_seen": _local(a["first_seen"])} for a in e.get("alerts", [])]
    else:
        out["n_alerts_any_scenario"] = len(e.get("alerts", []))
    return out


@router.get("/scorecard")
def scorecard():
    """Headline metrics (sensitivity, PPV, lead time, false alarms/week, Brier), per-scenario
    breakdowns and the scenario x event-type coverage matrix of the latest run."""
    d = store.load()
    return {"notice": NOTICE, **d["scorecard"]}


@router.get("/events")
def events():
    """Ground-truth events with detection status, lead time and severities."""
    d = store.load()
    return {"notice": NOTICE, "timezone": "Asia/Dubai",
            "events": [_event_out(e, with_alerts=False) for e in d.get("events", [])]}


@router.get("/calibration")
def calibration():
    """Calibration bins (predicted fused score vs observed event frequency) + Brier."""
    d = store.load()
    return {"notice": NOTICE, **d.get("calibration", {})}


@router.get("/replay/{event_id}")
def replay(event_id: str):
    """Hour-by-hour signal and fused-score traces around one event, plus the alerts fired."""
    d = store.load()
    ev = next((e for e in d.get("events", []) if e["id"] == event_id), None)
    if ev is None:
        raise HTTPException(404, detail=f"unknown event id {event_id!r}")
    tr = store.traces()
    hours = []
    if tr is not None:
        sub = tr[tr["event_id"] == event_id]
        if len(sub):
            wide = sub.pivot_table(index="ts_utc", columns="series", values="value", aggfunc="mean")
            for ts, row in wide.sort_index().iterrows():
                vals = {k: (None if v != v else round(float(v), 4)) for k, v in row.items()}
                hours.append({"ts": _local(ts),
                              "signals": {k: v for k, v in vals.items() if not k.startswith("fused:")},
                              "fused": {k[6:]: v for k, v in vals.items() if k.startswith("fused:")}})
    return {"notice": NOTICE, "timezone": "Asia/Dubai", "event": _event_out(ev), "hours": hours}
