"""Operations: route alerts to the responsible role through n8n, and audit it.

    POST /api/ops/replay_alert/{alert_id}?demo=true   (re)send an alert to the n8n webhook
    POST /api/ops/events                              n8n posts each routing step here
    GET  /api/ops/audit?alert_id=&since=&until=       acks + sends + routing steps, in order
    GET  /api/ops/plan/{alert_id}?demo=true           the steps n8n will run (for the diagram)
    GET  /api/ops/channels                            role -> channel map (env URLs masked)

The payload and plan come from packages/pipelines/ops/notify.py (vendored into
_vendor/sentinel_ops at deploy time), the same code the pipeline uses.

Ledgers (state dir; Azure: /home/data/sentinel-grid/alerts):
    acks.jsonl         acknowledgements (routers/alerts.py)
    sends.jsonl        pipeline sends (pulled from the data branch with the alert files)
    ops_sends.jsonl    sends made by this API (replay_alert)
    ops_events.jsonl   routing steps reported by n8n

Environment:
    N8N_WEBHOOK_URL     the n8n production webhook (e.g. an ngrok/cloudflared tunnel URL)
    OPS_CALLBACK_BASE   base URL n8n should call back (default: this request's base URL).
                        For n8n in Docker + a local API: http://host.docker.internal:8000
    OPS_CALLBACK_TOKEN  if set, /events requires header X-Ops-Token with this value

SECURITY GAP (hackathon): replay_alert has no auth (it only re-sends public alert text to
pre-configured channels); /events is protected only by the optional shared token.
"""
from __future__ import annotations

import hmac
import importlib.util
import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from routers import alerts as A

router = APIRouter()
API_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = API_DIR.parents[1] if API_DIR.name == "api" else None
DUBAI = ZoneInfo("Asia/Dubai")
STEPS = {"received", "routed", "logged", "notified_owner", "ack_wait", "acked", "escalated",
         "escalation_wait", "acked_after_escalation", "emirate_ops_lead", "error"}
_lock = threading.Lock()


def _notify():
    if "sentinel_ops_notify" in sys.modules:
        return sys.modules["sentinel_ops_notify"]
    for p in (API_DIR / "_vendor" / "sentinel_ops" / "notify.py",
              (REPO_ROOT / "packages" / "pipelines" / "ops" / "notify.py") if REPO_ROOT else None):
        if p is not None and p.is_file():
            spec = importlib.util.spec_from_file_location("sentinel_ops_notify", p)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
            return mod
    raise HTTPException(503, detail="routing code not deployed (_vendor/sentinel_ops missing)")


def _state() -> Path:
    return A._state_dir()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _local(ts: str | None) -> str | None:
    if not ts:
        return None
    t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(DUBAI).isoformat(timespec="seconds")


SEVERITIES = ("watch", "advisory", "warning")


def _alert_dict(alert_id: str, as_severity: str | None = None) -> dict:
    """The alert; ``as_severity`` rehearses its routing at another severity (demo), and
    the payload is then flagged rehearsal=true wherever it shows up."""
    r = A._find(alert_id)
    a = A._row_to_alert(r, full=True)
    if as_severity:
        if as_severity not in SEVERITIES:
            raise HTTPException(422, detail="as_severity must be watch, advisory or warning")
        a["true_severity"], a["severity"] = a["severity"], as_severity
    return a


def _rehearsal(p: dict, a: dict) -> dict:
    if a.get("true_severity"):
        p["rehearsal"] = True
        p["true_severity"] = a["true_severity"]
    return p


def _mask(ch: dict) -> dict:
    """Env-configured channel URLs can be secrets (Slack webhooks): show the host only."""
    out = dict(ch)
    if ch.get("url_source") == "env":
        out["url"] = f"https://{urlparse(ch['url']).netloc}/…"
    return out


def _public_payload(p: dict) -> dict:
    return {**{k: v for k, v in p.items() if k != "callback_token"},
            "channels": {k: _mask(v) for k, v in p["channels"].items()}}


def _callback_base(request: Request) -> str:
    return os.environ.get("OPS_CALLBACK_BASE") or str(request.base_url).rstrip("/")


@router.get("/channels")
def channels():
    n = _notify()
    cfg = n.load_channels()
    return {"groups": cfg["groups"],
            "roles": {r: {**_mask(n.channel_for(r, cfg)), "escalates_to": v.get("escalates_to")}
                      for r, v in cfg["roles"].items()},
            "inbox": cfg["demo_base"].replace("webhook.site/", "webhook.site/#!/view/"),
            "routing": cfg.get("routing", {})}


@router.get("/plan/{alert_id}")
def plan(alert_id: str, request: Request, demo: bool = Query(True),
         as_severity: str | None = Query(None)):
    n = _notify()
    a = _alert_dict(alert_id, as_severity)
    p = n.build_payload(a, n.load_channels(), callback_base=_callback_base(request),
                        demo=demo, source="plan")
    return _public_payload(_rehearsal(p, a))


@router.post("/replay_alert/{alert_id}")
def replay_alert(alert_id: str, request: Request, demo: bool = Query(True),
                 as_severity: str | None = Query(None, description="rehearse at another severity")):
    """Force-(re)send an existing alert to n8n. Never fails the caller: if the webhook is
    unset or unreachable the response says so (sent=false) and the dashboard simulates
    the routing instead, so a demo survives a dead network."""
    n = _notify()
    a = _alert_dict(alert_id, as_severity)
    payload = _rehearsal(n.build_payload(a, n.load_channels(), callback_base=_callback_base(request),
                                         demo=demo, source="api-replay"), a)
    url = os.environ.get("N8N_WEBHOOK_URL")
    if not url:
        ok, status, err = False, None, "N8N_WEBHOOK_URL not configured"
    else:
        ok, status, err = n.post_payload(payload, url, timeout=6)
        rec = n.send_record(payload, ok, status, err, url)
        with _lock:
            n.append_ledger(_state() / "ops_sends.jsonl", rec)
    return {"sent": ok, "http_status": status, "error": err,
            "n8n_host": urlparse(url).netloc if url else None,
            "simulate": not ok, "payload": _public_payload(payload)}


class OpsEvent(BaseModel):
    alert_id: str = Field(..., max_length=80)
    step: str = Field(..., max_length=40)
    status: str = Field("done", max_length=20)
    role: str | None = Field(None, max_length=40)
    channel: str | None = Field(None, max_length=300)
    detail: str | None = Field(None, max_length=500)
    execution_id: str | None = Field(None, max_length=40)


@router.post("/events", status_code=201)
def ops_event(ev: OpsEvent, x_ops_token: str | None = Header(None)):
    """Routing step callback from n8n."""
    token = os.environ.get("OPS_CALLBACK_TOKEN")
    if token and not (x_ops_token and hmac.compare_digest(x_ops_token, token)):
        raise HTTPException(401, detail="bad or missing X-Ops-Token")
    if ev.step not in STEPS:
        raise HTTPException(422, detail=f"unknown step {ev.step!r}")
    rec = {"ts": _now(), "kind": "route", **ev.model_dump()}
    if rec.get("channel"):
        rec["channel"] = urlparse(rec["channel"]).netloc or rec["channel"]
    n = _notify()
    with _lock:
        n.append_ledger(_state() / "ops_events.jsonl", rec)
    return {"ok": True}


@router.get("/audit")
def audit(alert_id: str | None = Query(None), since: str | None = Query(None),
          until: str | None = Query(None), limit: int = Query(500, le=5000)):
    """Acks, notification sends and n8n routing steps, oldest first (Asia/Dubai)."""
    n = _notify()
    rows = []
    for r in n.read_ledger(A.store.acks_path()):
        rows.append({"ts": r.get("acknowledged_at"), "kind": "ack", "alert_id": r.get("alert_id"),
                     "by": r.get("acknowledged_by"), "notes": r.get("notes"),
                     "auto": str(r.get("acknowledged_by", "")).startswith("n8n")})
    pipeline_sends = [d / "sends.jsonl" for d in A._candidates()]
    seen = set()
    for p in pipeline_sends + [_state() / "ops_sends.jsonl"]:
        if str(p) in seen:
            continue
        seen.add(str(p))
        rows += n.read_ledger(p)
    rows += n.read_ledger(_state() / "ops_events.jsonl")

    def utc(ts):
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)

    lo = utc(since) if since else None
    hi = utc(until) if until else None
    out = []
    for r in rows:
        if not r.get("ts") or (alert_id and r.get("alert_id") != alert_id):
            continue
        t = utc(r["ts"])
        if (lo and t < lo) or (hi and t > hi):
            continue
        out.append({**r, "ts": _local(r["ts"]), "_t": t})
    out.sort(key=lambda r: r["_t"])
    for r in out:
        r.pop("_t")
    return {"timezone": "Asia/Dubai", "count": len(out), "events": out[-limit:]}
