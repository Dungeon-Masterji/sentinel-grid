"""Tri-signal alerts (fusion detector output).

Files are produced by packages/pipelines/fusion/alerts.py, every 15 minutes by
.github/workflows/alert-refresh.yml, which force-pushes them to the orphan branch
`data/processed/alerts`:

    active.parquet  signal_traces.parquet  fused_traces.parquet  units.parquet  meta.json

Where the API reads them from (first hit wins):
  1. $ALERTS_DATA_DIR
  2. the state dir: files downloaded from the data branch (Azure: /home/data/sentinel-grid/
     alerts, which persists across deploys and restarts). Downloaded on cold start, then
     whenever older than ALERTS_TTL_SECONDS (default 600), or immediately on
     POST /api/alerts/refresh.
  3. ./data/alerts (copied in at deploy time by main_sentinel-grid-api.yml)
  4. <repo>/data/processed/alerts (local development)

Acknowledgements are appended to acks.jsonl in the state dir (locally:
data/processed/alerts/acks.jsonl).

SECURITY GAP (hackathon): /ack has NO authentication. Anyone who can reach the API can
acknowledge an alert under any name. /refresh is protected only by a shared secret header
(X-Refresh-Secret == $ALERTS_REFRESH_SECRET), and only re-pulls public files. Put real
auth (Entra ID / signed tokens) in front of both before production use.

Explainability (Chunk 4): every alert carries
  drivers               SHAP shares (%) from a LightGBM surrogate of the fused score
  drivers_prior         the static fusion-weight shares (%), for comparison
  drivers_method        "shap" or "fusion_weights" (fallback when no surrogate could be fitted)
  explanation_template  one-paragraph plain-English summary
GET /{id}/shap_waterfall returns the raw SHAP values for client-side waterfall charts.
SHAP is computed in the pipeline; the API only reads it (no lightgbm/shap at runtime).

LLM narration (Chunk 5): the pipeline also stores
  explanation_en / explanation_ar   Groq narration (gpt-oss-120b) + Arabic (allam-2-7b)
  explanation_source / explanation_ar_source   "llm" | "template_fallback"
  action                            {owner_role, time_window, specific_action,
                                     escalation_if_no_ack_in_minutes, source, model}
GET /{id}/regenerate_narration (X-Admin-Token == $ADMIN_TOKEN) re-runs the three LLM calls
here in the API, using the same code vendored into _vendor/sentinel_explain at deploy time,
and stores the result in narration_overrides.json in the state dir; overrides win over the
pipeline's text for that alert id. Needs GROQ_API_KEY (+ GROQ_MODEL, GROQ_MODEL_AR) in the
app settings. SECURITY GAP (hackathon): a single shared admin token, no user identity.

Times in responses are Asia/Dubai (ISO-8601, +04:00). Storage is UTC.
"""
from __future__ import annotations

import hmac
import importlib
import importlib.util
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

router = APIRouter()
log = logging.getLogger("sentinel.api.alerts")

API_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = API_DIR.parents[1] if API_DIR.name == "api" else None
DUBAI = ZoneInfo("Asia/Dubai")
FILES = ("active.parquet", "signal_traces.parquet", "fused_traces.parquet", "units.parquet",
         "meta.json")
DEFAULT_SOURCE = ("https://raw.githubusercontent.com/Dungeon-Masterji/sentinel-grid/"
                  "refs/heads/data/processed/alerts/data/processed/alerts")
NOTICE = ("Fused from synthetic clinical (Synthea) and synthetic workforce data plus real "
          "environmental observations. Decision support only.")
SEVERITY_RANK = {"watch": 0, "advisory": 1, "warning": 2}
JSON_FIELDS = ("drivers", "drivers_prior", "drivers_severity", "driver_details",
               "driver_onsets", "shap", "shap_severity", "shap_disagreements", "hex_boundary")


def _on_azure() -> bool:
    return bool(os.environ.get("WEBSITE_SITE_NAME"))


def _state_dir() -> Path:
    env = os.environ.get("ALERTS_STATE_DIR")
    if env:
        return Path(env)
    if _on_azure():
        return Path("/home/data/sentinel-grid/alerts")
    return (REPO_ROOT / "data" / "processed" / "alerts") if REPO_ROOT else API_DIR / "data" / "alerts"


def _source_url() -> str | None:
    url = os.environ.get("ALERTS_SOURCE_URL")
    if url:
        return url.rstrip("/")
    return DEFAULT_SOURCE if _on_azure() else None   # locally: use files on disk


def _candidates() -> list[Path]:
    out = []
    if os.environ.get("ALERTS_DATA_DIR"):
        out.append(Path(os.environ["ALERTS_DATA_DIR"]))
    out.append(_state_dir())
    out.append(API_DIR / "data" / "alerts")
    if REPO_ROOT is not None:
        out.append(REPO_ROOT / "data" / "processed" / "alerts")
    seen, uniq = set(), []
    for p in out:
        if str(p) not in seen:
            seen.add(str(p))
            uniq.append(p)
    return uniq


class _Store:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ack_lock = threading.Lock()
        self._key = None
        self.active = self.sig = self.fused = self.units = None
        self.meta: dict = {}
        self.source_dir: Path | None = None
        self.last_pull: float = 0.0
        self.last_pull_status: str = "never"

    # -- remote pull --------------------------------------------------------------------
    def pull(self, force: bool = False) -> str:
        url = _source_url()
        if not url:
            return "disabled (no ALERTS_SOURCE_URL; local files)"
        ttl = float(os.environ.get("ALERTS_TTL_SECONDS", "600"))
        if not force and time.time() - self.last_pull < ttl:
            return self.last_pull_status
        import requests
        dest = _state_dir()
        dest.mkdir(parents=True, exist_ok=True)
        self.last_pull = time.time()        # also throttles retries after a failure
        try:
            fetched = {}
            for name in FILES:
                r = requests.get(f"{url}/{name}", timeout=10)
                r.raise_for_status()
                fetched[name] = r.content
            for name, body in fetched.items():   # write all-or-nothing
                tmp = dest / f".{name}.tmp"
                tmp.write_bytes(body)
                os.replace(tmp, dest / name)
            self.last_pull_status = f"ok {datetime.now(timezone.utc).isoformat(timespec='seconds')}"
        except Exception as e:  # noqa: BLE001 - keep serving the last good copy
            self.last_pull_status = f"failed: {e.__class__.__name__}: {e}"[:300]
            log.warning("alert pull failed: %s", self.last_pull_status)
        return self.last_pull_status

    # -- local load -----------------------------------------------------------------------
    def load(self):
        self.pull()
        d = next((p for p in _candidates() if (p / "active.parquet").is_file()), None)
        if d is None:
            raise HTTPException(503, detail={"error": "alerts not available yet",
                                              "hint": "run python -m packages.pipelines.fusion.alerts"})
        paths = [d / n for n in FILES if (d / n).is_file()]
        key = tuple((str(p), p.stat().st_mtime) for p in paths)
        with self._lock:
            if key != self._key:
                import pandas as pd
                self.active = pd.read_parquet(d / "active.parquet")
                self.sig = (pd.read_parquet(d / "signal_traces.parquet")
                            if (d / "signal_traces.parquet").is_file() else None)
                self.fused = (pd.read_parquet(d / "fused_traces.parquet")
                              if (d / "fused_traces.parquet").is_file() else None)
                self.units = (pd.read_parquet(d / "units.parquet")
                              if (d / "units.parquet").is_file() else None)
                self.meta = (json.loads((d / "meta.json").read_text())
                             if (d / "meta.json").is_file() else {})
                self.source_dir = d
                self._key = key
        return self.active

    # -- acks -----------------------------------------------------------------------------
    def acks_path(self) -> Path:
        return _state_dir() / "acks.jsonl"

    def append_ack(self, rec: dict) -> None:
        p = self.acks_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with self._ack_lock, open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def acks_for(self, alert_id: str) -> list[dict]:
        p = self.acks_path()
        if not p.is_file():
            return []
        out = []
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("alert_id") == alert_id:
                    out.append(r)
        return out


store = _Store()


# -- LLM narration overrides (regenerate_narration) -------------------------------------
NARRATION_FIELDS = ("explanation_en", "explanation_ar", "explanation_source",
                    "explanation_ar_source", "action", "narration")
_override_lock = threading.Lock()
_override_cache: dict = {"key": None, "data": {}}


def _overrides_path() -> Path:
    return _state_dir() / "narration_overrides.json"


def _overrides() -> dict:
    p = _overrides_path()
    if not p.is_file():
        return {}
    key = p.stat().st_mtime
    with _override_lock:
        if _override_cache["key"] != key:
            try:
                _override_cache["data"] = json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                _override_cache["data"] = {}
            _override_cache["key"] = key
        return _override_cache["data"]


def _save_override(alert_id: str, rec: dict) -> None:
    p = _overrides_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with _override_lock:
        data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
        data[alert_id] = rec
        if len(data) > 500:                     # keep the newest 500
            data = dict(sorted(data.items(), key=lambda kv: kv[1].get("regenerated_at", ""))[-500:])
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
        _override_cache["key"] = None


def _explain_pkg_dirs() -> list[Path]:
    out = [API_DIR / "_vendor" / "sentinel_explain"]
    if REPO_ROOT is not None:
        out.append(REPO_ROOT / "packages" / "pipelines" / "explain")
    return out


def _llm():
    """packages/pipelines/explain loaded as package 'sentinel_explain' (vendored on Azure)."""
    if "sentinel_explain.llm" in sys.modules:
        return sys.modules["sentinel_explain.llm"]
    for d in _explain_pkg_dirs():
        if (d / "llm.py").is_file():
            spec = importlib.util.spec_from_file_location(
                "sentinel_explain", d / "__init__.py", submodule_search_locations=[str(d)])
            mod = importlib.util.module_from_spec(spec)
            sys.modules["sentinel_explain"] = mod
            spec.loader.exec_module(mod)
            return importlib.import_module("sentinel_explain.llm")
    raise HTTPException(503, detail="narration code not deployed (_vendor/sentinel_explain missing)")


def _scenarios_cfg() -> dict:
    p = API_DIR / "_vendor" / "scenarios.json"
    if p.is_file():
        return json.loads(p.read_text(encoding="utf-8"))
    if REPO_ROOT is not None and (REPO_ROOT / "configs" / "scenarios.yaml").is_file():
        import yaml
        return yaml.safe_load((REPO_ROOT / "configs" / "scenarios.yaml").read_text())
    raise HTTPException(503, detail="scenario config not deployed (_vendor/scenarios.json missing)")


def _local(ts) -> str | None:
    import pandas as pd
    if ts is None or (isinstance(ts, float) and ts != ts):
        return None
    t = pd.Timestamp(ts)
    t = t.tz_localize("UTC") if t.tzinfo is None else t
    return t.tz_convert(DUBAI).isoformat()


def _now_utc():
    import pandas as pd
    return pd.Timestamp(datetime.now(timezone.utc))


def _json(r, col, default=None):
    """Decode a JSON column; tolerate parquet files written before the column existed."""
    v = r.get(col) if hasattr(r, "get") else None
    if v is None or (isinstance(v, float) and v != v):
        return default
    try:
        out = json.loads(v)
    except (TypeError, ValueError):
        return default
    return default if out is None else out


def _row_to_alert(r, *, full: bool = False) -> dict:
    drivers = _json(r, "drivers", {})
    d = {
        "id": r["id"], "scenario_id": r["scenario_id"], "scenario_label": r["scenario_label"],
        "severity": r["severity"], "h3_cell": r["h3_cell"],
        "sites_affected": list(r["sites_affected"]), "site_names": list(r["site_names"]),
        "emirates": list(r["emirates"]), "clinical_catchment": r["clinical_catchment"],
        "score": float(r["score"]), "credible_interval": [float(x) for x in r["credible_interval"]],
        "coverage": float(r["coverage"]),
        "drivers": drivers, "drivers_prior": _json(r, "drivers_prior", drivers),
        "drivers_method": (r.get("drivers_method") or "fusion_weights"),
        "explanation_template": r.get("explanation_template"),
        "explanation_en": r.get("explanation_en") or r.get("explanation_template"),
        "explanation_ar": r.get("explanation_ar"),
        "explanation_source": r.get("explanation_source") or "template_fallback",
        "explanation_ar_source": r.get("explanation_ar_source"),
        "action": _json(r, "action"),
        "driver_details": _json(r, "driver_details", {}),
        "created_at": _local(r["created_at"]), "peak_at": _local(r["peak_at"]),
        "window_start": _local(r["window_start"]), "expires_at": _local(r["expires_at"]),
        "as_of": _local(r["as_of"]), "action_owner": r["action_owner"],
        "recommended_actions": list(r["recommended_actions"]),
    }
    if full:
        shap = _json(r, "shap")
        d["drivers_severity"] = _json(r, "drivers_severity")
        d["driver_onsets"] = _json(r, "driver_onsets", {})
        d["shap_disagreements"] = _json(r, "shap_disagreements", [])
        d["shap"] = None if shap is None else {
            "signals": shap.get("signals"), "base_value": shap.get("base_value"),
            "prediction": shap.get("prediction"), "model": shap.get("model"),
            "background_rows": shap.get("background_rows")}
        d["footprint"] = {"type": "Feature", "geometry": json.loads(r["hex_boundary"]),
                          "properties": {"h3_cell": r["h3_cell"], "resolution": 7}}
        d["narration"] = _json(r, "narration")
    ov = _overrides().get(d["id"])
    if ov:
        d.update({k: ov[k] for k in NARRATION_FIELDS if k in ov and (full or k != "narration")})
    return d


def _envelope(**payload) -> dict:
    return {"notice": NOTICE, "timezone": "Asia/Dubai",
            "as_of": _local(store.meta.get("as_of")) if store.meta.get("as_of") else None,
            "mode": store.meta.get("mode"), **payload}


@router.get("")
def list_alerts(severity: str | None = Query(None, description="minimum severity: watch|advisory|warning"),
                scenario_id: str | None = Query(None),
                site_id: str | None = Query(None),
                include_expired: bool = Query(False)):
    """Active alerts, most severe first."""
    df = store.load()
    if severity is not None and severity not in SEVERITY_RANK:
        raise HTTPException(422, detail="severity must be watch, advisory or warning")
    if not include_expired and len(df):
        df = df[df["expires_at"] > _now_utc()]
    if severity:
        df = df[df["severity"].map(SEVERITY_RANK) >= SEVERITY_RANK[severity]]
    if scenario_id:
        df = df[df["scenario_id"] == scenario_id]
    if site_id:
        df = df[df["sites_affected"].map(lambda s: site_id in list(s))]
    if len(df):
        df = df.assign(_r=df["severity"].map(SEVERITY_RANK)).sort_values(
            ["_r", "score", "peak_at"], ascending=[False, False, False])
    alerts = [_row_to_alert(r) for _, r in df.iterrows()]
    return _envelope(count=len(alerts), alerts=alerts,
                     scenarios=sorted(store.active["scenario_id"].unique().tolist()) if len(store.active) else [])


def _traces(alert: dict) -> dict:
    """24 h traces (Asia/Dubai) for each driver signal at its source hex, plus the fused score."""
    import pandas as pd
    out = {"signals": {}, "fused": []}
    end = pd.Timestamp(alert["peak_at"]).tz_convert("UTC") if alert.get("peak_at") else _now_utc()
    lo = end - pd.Timedelta(hours=24)
    units = store.units
    u = units[units["h3"] == alert["h3_cell"]].iloc[0] if units is not None and len(units) else None
    if store.sig is not None:
        for name, det in alert["driver_details"].items():
            src = alert["h3_cell"]
            if u is not None:
                src = {"env": u["env_h3"], "clin": u["clinical_h3"]}.get(det["pillar"], alert["h3_cell"])
            s = store.sig[(store.sig["signal"] == name) & (store.sig["h3"] == src)
                          & (store.sig["ts_utc"] > lo) & (store.sig["ts_utc"] <= end)]
            out["signals"][name] = [{"ts": _local(r.ts_utc), "value": _f(r.value),
                                     "baseline": _f(r.baseline), "score": _f(r.score)}
                                    for r in s.sort_values("ts_utc").itertuples()]
    if store.fused is not None:
        f = store.fused[(store.fused["scenario_id"] == alert["scenario_id"])
                        & (store.fused["unit_h3"] == alert["h3_cell"])
                        & (store.fused["ts_utc"] > lo) & (store.fused["ts_utc"] <= end)]
        out["fused"] = [{"ts": _local(r.ts_utc), "mean": _f(r.mean), "ci_low": _f(r.ci_low),
                         "ci_high": _f(r.ci_high)} for r in f.sort_values("ts_utc").itertuples()]
    return out


def _f(x):
    try:
        v = float(x)
        return None if v != v else round(v, 4)
    except (TypeError, ValueError):
        return None


def _find(alert_id: str):
    df = store.load()
    m = df[df["id"] == alert_id]
    if m.empty:
        raise HTTPException(404, detail=f"unknown alert id {alert_id!r}")
    return m.iloc[0]


@router.get("/{alert_id}")
def alert_detail(alert_id: str):
    """Full alert: drivers breakdown, hex footprint (GeoJSON), 24 h traces, acks."""
    r = _find(alert_id)
    a = _row_to_alert(r, full=True)
    a["traces"] = _traces(a)
    a["acks"] = store.acks_for(alert_id)
    a["expired"] = bool(r["expires_at"] <= _now_utc())
    return _envelope(alert=a)


@router.get("/{alert_id}/shap_waterfall")
def shap_waterfall(alert_id: str,
                   level: str = Query("signal", description="signal (lags collapsed) | feature (raw columns)"),
                   task: str = Query("regression", description="regression (fused score) | classification (log-odds of firing)")):
    """Raw SHAP values for a client-side waterfall: base_value -> ... -> prediction.

    Steps are ordered by |shap|. Each has feature, shap, value (the input), baseline (the
    running total before the step) and cumulative (after it)."""
    if level not in ("signal", "feature"):
        raise HTTPException(422, detail="level must be signal or feature")
    if task not in ("regression", "classification"):
        raise HTTPException(422, detail="task must be regression or classification")
    r = _find(alert_id)
    payload = _json(r, "shap" if task == "regression" else "shap_severity")
    if not payload:
        raise HTTPException(404, detail={"error": f"no SHAP ({task}) for this alert",
                                          "drivers_method": r.get("drivers_method") or "fusion_weights"})
    feats = payload.get("features") or {}
    xs = payload.get("x") or {}
    details = _json(r, "driver_details", {})
    if level == "signal":
        items: dict[str, float] = {}
        for k, v in feats.items():
            sig = k.split("__", 1)[0]
            items[sig] = items.get(sig, 0.0) + float(v)
        values = {k: xs.get(f"{k}__value") for k in items}
    else:
        items = {k: float(v) for k, v in feats.items()}
        values = {k: xs.get(k) for k in items}
    run = float(payload["base_value"])
    steps = []
    for k, v in sorted(items.items(), key=lambda kv: -abs(kv[1])):
        sig = k.split("__", 1)[0]
        steps.append({"feature": k, "signal": sig, "pillar": (details.get(sig) or {}).get("pillar"),
                      "shap": round(v, 6), "value": values[k], "baseline": round(run, 6),
                      "cumulative": round(run + v, 6)})
        run += v
    return _envelope(alert_id=alert_id, task=task, level=level, output=payload.get("output"),
                     base_value=payload["base_value"], prediction=round(run, 6),
                     model=payload.get("model"), steps=steps)


@router.get("/{alert_id}/regenerate_narration")
def regenerate_narration(alert_id: str, x_admin_token: str | None = Header(None)):
    """Force fresh LLM narration + Arabic + action for one alert (admin only).

    GET because the spec asks for it; it is NOT idempotent (it spends Groq tokens)."""
    token = os.environ.get("ADMIN_TOKEN")
    if not token:
        raise HTTPException(503, detail="admin endpoint not configured (ADMIN_TOKEN unset)")
    if not x_admin_token or not hmac.compare_digest(x_admin_token, token):
        raise HTTPException(401, detail="bad or missing X-Admin-Token")
    r = _find(alert_id)
    alert = _row_to_alert(r, full=True)
    alert["shap"] = _json(r, "shap")
    cfg = _scenarios_cfg()
    scenario = next((s for s in cfg["scenarios"] if s["id"] == alert["scenario_id"]), None)
    if scenario is None:
        raise HTTPException(409, detail=f"scenario {alert['scenario_id']!r} not in deployed config")
    llm = _llm()
    gc = sys.modules["sentinel_explain.groq_client"]
    cache = gc.NarrationCache(_state_dir() / "narration_cache.sqlite")
    try:
        state = llm.RunState(llm.narration_config(cfg), cache)
        llm.enrich_alert(alert, scenario, state=state, force=True)
    finally:
        cache.close()
    if alert["explanation_source"] != "llm":
        # don't replace a good pipeline narration with a template
        raise HTTPException(502, detail={"error": "LLM narration failed; existing text kept",
                                          "narration": alert.get("narration")})
    rec ={k: alert[k] for k in NARRATION_FIELDS}
    rec["regenerated_at"] = datetime.now(DUBAI).isoformat(timespec="seconds")
    _save_override(alert_id, rec)
    return _envelope(alert_id=alert_id, **rec)


class AckBody(BaseModel):
    acknowledged_by: str = Field(..., min_length=1, max_length=120)
    notes: str = Field("", max_length=2000)


@router.post("/{alert_id}/ack", status_code=201)
def ack_alert(alert_id: str, body: AckBody):
    """Record an acknowledgement. NO AUTH (hackathon gap, see module docstring)."""
    r = _find(alert_id)
    rec = {"alert_id": alert_id, "scenario_id": r["scenario_id"], "h3_cell": r["h3_cell"],
           "severity": r["severity"], "acknowledged_by": body.acknowledged_by.strip(),
           "notes": body.notes.strip(),
           "acknowledged_at": datetime.now(DUBAI).isoformat(timespec="seconds"),
           "authenticated": False}
    store.append_ack(rec)
    return {"ok": True, "ack": rec}


@router.post("/refresh")
def refresh(x_refresh_secret: str | None = Header(None)):
    """Re-pull alert files from the data branch now (called by alert-refresh.yml)."""
    secret = os.environ.get("ALERTS_REFRESH_SECRET")
    if not secret:
        raise HTTPException(503, detail="refresh webhook not configured (ALERTS_REFRESH_SECRET unset)")
    if not x_refresh_secret or not hmac.compare_digest(x_refresh_secret, secret):
        raise HTTPException(401, detail="bad or missing X-Refresh-Secret")
    status = store.pull(force=True)
    store.load()
    return {"ok": status.startswith("ok") or status.startswith("disabled"), "pull": status,
            "as_of": store.meta.get("as_of"), "n_alerts": int(len(store.active))}
