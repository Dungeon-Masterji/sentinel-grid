"""Alert routing to n8n: payload, channel resolution, routing plan, send + ledger.

Shared by the pipeline (fusion/alerts.py pushes NEW alerts after each run) and the API
(POST /api/ops/replay_alert/{id}, vendored into apps/api/_vendor at deploy time), so
both send exactly the same body.

    payload = build_payload(alert, load_channels(), callback_base="https://…api", demo=True)
    ok, status, err = post_payload(payload, os.environ["N8N_WEBHOOK_URL"])

The n8n workflow (ops/n8n/workflows/alert_routing.json) only routes: who to notify comes
resolved in the payload (configs/ops_channels.yaml), so the YAML stays the single source
of truth and the workflow never needs editing when a channel changes.

Routing (mirrored by routing_plan() for the /ops diagram):
  watch     log only: n8n acknowledges as "n8n-auto-log"
  advisory  notify owner -> wait 15 min -> acked? done : escalate (as warning)
  warning   notify owner + their escalation contact -> wait escalation_if_no_ack_in_minutes
            -> acked? done : notify emirate operations lead
With demo=True every wait is routing.demo_wait_seconds (10 s), so a live demo completes.

Only the CLI-free functions here read the clock (for ledger timestamps).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("sentinel.ops.notify")

HERE = Path(__file__).resolve()
CANDIDATE_CONFIGS = [
    Path(os.environ["OPS_CHANNELS_PATH"]) if os.environ.get("OPS_CHANNELS_PATH") else None,
    HERE.parent / "ops_channels.json",                          # vendored copy (API on Azure)
    HERE.parents[3] / "configs" / "ops_channels.yaml" if len(HERE.parents) > 3 else None,
]
FALLBACK_ROLE = "emirate_ops_lead"
SEVERITY_RANK = {"watch": 0, "advisory": 1, "warning": 2}


def load_channels(path: Path | str | None = None) -> dict:
    for p in ([Path(path)] if path else [c for c in CANDIDATE_CONFIGS if c is not None]):
        if p.is_file():
            if p.suffix == ".json":
                return json.loads(p.read_text())
            import yaml
            return yaml.safe_load(p.read_text())
    raise FileNotFoundError("ops_channels config not found (configs/ops_channels.yaml)")


def channel_for(role: str | None, cfg: dict) -> dict | None:
    if role is None:
        return None
    roles = cfg["roles"]
    if role not in roles:
        log.warning("unknown owner role %r; routing to %s", role, FALLBACK_ROLE)
        role = FALLBACK_ROLE
    r = roles[role]
    g = cfg["groups"][r["group"]]
    env_url = os.environ.get(r.get("url_env") or "")
    return {"role": role, "label": r["label"], "group": r["group"], "group_label": g["label"],
            "color": g["color"],
            "url": env_url or f"{cfg['demo_base'].rstrip('/')}/{role}",
            "url_source": "env" if env_url else "demo"}


def escalation_role(role: str, cfg: dict) -> str:
    r = cfg["roles"].get(role) or {}
    return r.get("escalates_to") or FALLBACK_ROLE


def waits(severity: str, action: dict | None, cfg: dict, demo: bool) -> dict:
    rt = cfg.get("routing") or {}
    if demo:
        s = int(rt.get("demo_wait_seconds", 10))
        return {"ack_wait_s": s, "escalation_wait_s": s}
    esc = int((action or {}).get("escalation_if_no_ack_in_minutes") or 60)
    return {"ack_wait_s": int(rt.get("advisory_ack_wait_minutes", 15)) * 60,
            "escalation_wait_s": esc * 60}


def routing_plan(severity: str, owner: dict, supervisor: dict, lead: dict, w: dict) -> list[dict]:
    """The steps n8n will run for this alert, in order (ids match the events n8n posts)."""
    steps = [{"id": "received", "label": "n8n webhook received"},
             {"id": "routed", "label": f"Severity router → {severity}"}]
    if severity == "watch":
        return steps + [{"id": "logged", "label": "Logged only (auto-ack)", "terminal": True}]
    if severity == "advisory":
        steps += [{"id": "notified_owner", "label": f"Notify {owner['label']}", "role": owner["role"],
                   "color": owner["color"]},
                  {"id": "ack_wait", "label": f"Wait {_dur(w['ack_wait_s'])} for ack", "wait_s": w["ack_wait_s"]},
                  {"id": "acked", "label": "Acknowledged → done", "terminal": True, "branch": "ok"}]
    steps += [{"id": "escalated", "label": f"Notify {owner['label']} + {supervisor['label']}",
               "role": supervisor["role"], "color": supervisor["color"], "branch": "escalate"},
              {"id": "escalation_wait", "label": f"Wait {_dur(w['escalation_wait_s'])} for ack",
               "wait_s": w["escalation_wait_s"]},
              {"id": "acked_after_escalation", "label": "Acknowledged → done", "terminal": True, "branch": "ok"},
              {"id": "emirate_ops_lead", "label": f"Escalate to {lead['label']}", "role": lead["role"],
               "color": lead["color"], "terminal": True, "branch": "timeout"}]
    return steps


def _dur(s: int) -> str:
    return f"{s} s" if s < 120 else f"{s // 60} min"


def build_payload(alert: dict, cfg: dict, *, callback_base: str | None, demo: bool = False,
                  source: str = "pipeline") -> dict:
    """The body POSTed to the n8n webhook (spec fields + resolved channels + plan)."""
    sev = alert.get("severity", "watch")
    action = alert.get("action") or {}
    owner = channel_for(action.get("owner_role") or FALLBACK_ROLE, cfg)
    supervisor = channel_for(escalation_role(owner["role"], cfg), cfg)
    lead = channel_for(FALLBACK_ROLE, cfg)
    w = waits(sev, action, cfg, demo)
    names = [n for n in (alert.get("site_names") or []) if n]
    return {
        "alert_id": alert["id"],
        "scenario_id": alert.get("scenario_id"),
        "scenario_label": alert.get("scenario_label"),
        "severity": sev,
        "action": {k: action.get(k) for k in ("owner_role", "specific_action", "time_window",
                                               "escalation_if_no_ack_in_minutes")},
        "location_name": ", ".join(names) or alert.get("h3_cell"),
        "explanation_en": alert.get("explanation_en") or alert.get("explanation_template"),
        "channels": {"owner": owner, "supervisor": supervisor, "emirate_ops_lead": lead},
        "waits": w,
        "plan": routing_plan(sev, owner, supervisor, lead, w),
        "callback_base": (callback_base or "").rstrip("/") or None,
        "callback_token": os.environ.get("OPS_CALLBACK_TOKEN") or None,   # only n8n sees this
        "demo": bool(demo),
        "source": source,
        "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def post_payload(payload: dict, url: str, timeout: float = 8.0) -> tuple[bool, int | None, str | None]:
    import requests
    try:
        r = requests.post(url, json=payload, timeout=timeout,
                          headers={"User-Agent": "sentinel-grid-ops/1.0"})
        return r.status_code < 300, r.status_code, None if r.status_code < 300 else r.text[:200]
    except requests.RequestException as e:
        return False, None, f"{e.__class__.__name__}: {e}"[:200]


def append_ledger(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read_ledger(path: Path) -> list[dict]:
    if not Path(path).is_file():
        return []
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def send_record(payload: dict, ok: bool, status: int | None, err: str | None, url: str) -> dict:
    from urllib.parse import urlparse
    return {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": "send",
            "alert_id": payload["alert_id"], "severity": payload["severity"],
            "scenario_id": payload["scenario_id"], "owner_role": payload["channels"]["owner"]["role"],
            "source": payload["source"], "demo": payload["demo"], "rehearsal": payload.get("rehearsal", False),
            "ok": ok, "http_status": status,
            "error": err, "target": urlparse(url).netloc}


def send_new_alerts(alerts: list[dict], ledger_path: Path, *, url: str | None = None,
                    callback_base: str | None = None, cfg: dict | None = None) -> dict:
    """POST every alert not yet routed at its current severity (escalations re-send).
    With no N8N_WEBHOOK_URL: skip (logged), nothing written."""
    url = url or os.environ.get("N8N_WEBHOOK_URL")
    if not url:
        log.info("N8N_WEBHOOK_URL unset: %d alert(s) not routed to n8n", len(alerts))
        return {"enabled": False, "sent": 0, "failed": 0, "skipped": len(alerts)}
    cfg = cfg or load_channels()
    done = {(r["alert_id"], r["severity"]) for r in read_ledger(ledger_path) if r.get("ok")}
    sent = failed = skipped = 0
    for a in alerts:
        if (a["id"], a["severity"]) in done:
            skipped += 1
            continue
        payload = build_payload(a, cfg, callback_base=callback_base or os.environ.get("OPS_CALLBACK_BASE"),
                                source="pipeline")
        ok, status, err = post_payload(payload, url)
        append_ledger(Path(ledger_path), send_record(payload, ok, status, err, url))
        sent += ok
        failed += not ok
        if not ok:
            log.warning("n8n webhook failed for %s: %s %s", a["id"], status, err)
    log.info("n8n routing: sent %d, failed %d, already routed %d", sent, failed, skipped)
    return {"enabled": True, "sent": sent, "failed": failed, "skipped": skipped}
