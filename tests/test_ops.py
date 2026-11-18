"""Alert routing (packages/pipelines/ops/notify.py) and /api/ops. Offline: HTTP is mocked."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from packages.pipelines.ops import notify as N

ROOT = Path(__file__).resolve().parents[1]
CFG = N.load_channels(ROOT / "configs" / "ops_channels.yaml")


def alert(sev="warning", role="safety_officer", **kw):
    a = {"id": "al-1", "scenario_id": "heat-stress-cluster", "scenario_label": "Heat-stress cluster",
         "severity": sev, "h3_cell": "87x", "site_names": ["Al Quoz", "Jebel Ali Port"],
         "explanation_en": "A heat-stress cluster advisory is active.",
         "action": {"owner_role": role, "specific_action": "Move heavy tasks before 10:00.",
                    "time_window": "within_1h", "escalation_if_no_ack_in_minutes": 30,
                    "source": "llm", "model": "m"}}
    a.update(kw)
    return a


def test_every_role_has_a_group_and_escalation_ends_at_the_emirate_lead():
    for role, r in CFG["roles"].items():
        assert r["group"] in CFG["groups"]
        seen, cur = set(), role
        while cur != "emirate_ops_lead":           # no cycles, always terminates
            assert cur not in seen
            seen.add(cur)
            cur = N.escalation_role(cur, CFG)
    from packages.pipelines.explain.prompts import OWNER_ROLES
    assert set(OWNER_ROLES) <= set(CFG["roles"])   # every structured-action owner is routable
    for r in ("site_supervisor", "mohre_inspector", "doh_surveillance", "school_authority", "emirate_ops_lead"):
        assert r in CFG["roles"]


def test_payload_has_spec_fields_and_resolved_channels(monkeypatch):
    monkeypatch.delenv("OPS_CH_SAFETY_OFFICER", raising=False)
    p = N.build_payload(alert(), CFG, callback_base="http://api/", demo=False)
    for k in ("alert_id", "scenario_id", "severity", "action", "location_name", "explanation_en"):
        assert k in p
    assert set(p["action"]) == {"owner_role", "specific_action", "time_window", "escalation_if_no_ack_in_minutes"}
    assert p["location_name"] == "Al Quoz, Jebel Ali Port"
    assert p["channels"]["owner"]["role"] == "safety_officer"
    assert p["channels"]["supervisor"]["role"] == "site_supervisor"          # escalates_to
    assert p["channels"]["emirate_ops_lead"]["role"] == "emirate_ops_lead"
    assert p["channels"]["owner"]["url"].endswith("/safety_officer") and p["channels"]["owner"]["url_source"] == "demo"
    assert p["waits"] == {"ack_wait_s": 900, "escalation_wait_s": 1800}
    assert p["callback_base"] == "http://api"


def test_env_channel_overrides_demo(monkeypatch):
    monkeypatch.setenv("OPS_CH_SAFETY_OFFICER", "https://hooks.slack.com/services/T/B/secret")
    assert N.channel_for("safety_officer", CFG)["url_source"] == "env"


@pytest.mark.parametrize("sev, steps", [
    ("watch", ["received", "routed", "logged"]),
    ("advisory", ["received", "routed", "notified_owner", "ack_wait", "acked", "escalated", "escalation_wait",
                  "acked_after_escalation", "emirate_ops_lead"]),
    ("warning", ["received", "routed", "escalated", "escalation_wait", "acked_after_escalation", "emirate_ops_lead"]),
])
def test_plan_per_severity(sev, steps):
    p = N.build_payload(alert(sev), CFG, callback_base=None, demo=True)
    assert [s["id"] for s in p["plan"]] == steps
    assert all(s["wait_s"] == 10 for s in p["plan"] if "wait_s" in s)        # demo waits


def test_unknown_role_routes_to_emirate_lead():
    p = N.build_payload(alert(role="ceo"), CFG, callback_base=None)
    assert p["channels"]["owner"]["role"] == "emirate_ops_lead"


class FakeResp:
    def __init__(self, code):
        self.status_code, self.text = code, ""


def test_send_new_alerts_skips_without_url(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("requests.post", lambda *a, **k: calls.append(1))
    out = N.send_new_alerts([alert()], tmp_path / "sends.jsonl")
    assert out == {"enabled": False, "sent": 0, "failed": 0, "skipped": 1}
    assert calls == [] and not (tmp_path / "sends.jsonl").exists()


def test_send_new_alerts_dedupes_and_resends_on_escalation(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr("requests.post", lambda url, json=None, **k: sent.append(json) or FakeResp(200))
    led = tmp_path / "sends.jsonl"
    N.send_new_alerts([alert("advisory")], led, url="http://n8n/webhook/x", cfg=CFG)
    N.send_new_alerts([alert("advisory")], led, url="http://n8n/webhook/x", cfg=CFG)   # same severity: no resend
    N.send_new_alerts([alert("warning")], led, url="http://n8n/webhook/x", cfg=CFG)    # escalated: resend
    assert [p["severity"] for p in sent] == ["advisory", "warning"]
    recs = N.read_ledger(led)
    assert [r["severity"] for r in recs] == ["advisory", "warning"] and all(r["ok"] for r in recs)
    assert recs[0]["target"] == "n8n"


def test_failed_send_is_logged_and_retried(tmp_path, monkeypatch):
    codes = iter([500, 200])
    monkeypatch.setattr("requests.post", lambda *a, **k: FakeResp(next(codes)))
    led = tmp_path / "sends.jsonl"
    assert N.send_new_alerts([alert()], led, url="http://n8n/x", cfg=CFG)["failed"] == 1
    assert N.send_new_alerts([alert()], led, url="http://n8n/x", cfg=CFG)["sent"] == 1


def test_workflow_json_is_current_and_well_formed():
    wf = json.loads((ROOT / "ops" / "n8n" / "workflows" / "alert_routing.json").read_text())
    names = {n["name"] for n in wf["nodes"]}
    for k in wf["connections"]:
        assert k in names
        for outs in wf["connections"][k]["main"]:
            for c in outs:
                assert c["node"] in names
    hook = next(n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.webhook")
    assert hook["parameters"]["path"] == "sentinel-grid/alert"
    spec = importlib.util.spec_from_file_location("build_wf", ROOT / "ops" / "n8n" / "build_workflow.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.build() == wf, "alert_routing.json is stale: run python ops/n8n/build_workflow.py"


# ---------------------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------------------
@pytest.fixture
def api(tmp_path, monkeypatch):
    """The real alerts + ops routers on one alert parquet written by alerts_frame."""
    import pandas as pd
    from packages.pipelines.fusion.alerts import alerts_frame
    a = {**alert("advisory"), "sites_affected": ["s1"], "emirates": ["Dubai"], "clinical_catchment": "Dubai",
         "score": 0.5, "credible_interval": [0.3, 0.7], "coverage": 1.0, "drivers": {"apparent_temp": 100.0},
         "drivers_prior": {}, "drivers_method": "shap", "driver_details": {}, "driver_onsets": {},
         "shap": None, "shap_severity": None, "shap_disagreements": [], "explanation_template": "t",
         "explanation_ar": "ع", "explanation_source": "llm", "explanation_ar_source": "llm", "narration": {},
         "created_at": "2026-11-18T05:00:00Z", "peak_at": "2026-11-18T05:00:00Z",
         "window_start": "2026-11-18T00:00:00Z", "expires_at": "2030-01-01T00:00:00Z",
         "as_of": "2026-11-18T05:00:00Z", "action_owner": "site supervisor", "recommended_actions": ["x"],
         "hex_boundary": {"type": "Polygon", "coordinates": []}}
    data = tmp_path / "alerts"
    data.mkdir()
    alerts_frame([a]).to_parquet(data / "active.parquet")
    (data / "sends.jsonl").write_text(json.dumps({"ts": "2026-11-18T05:01:00+00:00", "kind": "send",
                                                  "alert_id": "al-1", "severity": "advisory",
                                                  "source": "pipeline", "ok": True}) + "\n")
    for k, v in {"ALERTS_DATA_DIR": str(data), "ALERTS_STATE_DIR": str(tmp_path / "state")}.items():
        monkeypatch.setenv(k, v)
    for k in ("WEBSITE_SITE_NAME", "N8N_WEBHOOK_URL", "OPS_CALLBACK_TOKEN", "OPS_CALLBACK_BASE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.syspath_prepend(str(ROOT / "apps" / "api"))
    for m in [m for m in sys.modules if m == "routers" or m.startswith("routers.")]:
        monkeypatch.delitem(sys.modules, m)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routers.alerts as ra
    import routers.ops as ro
    app = FastAPI()
    app.include_router(ra.router, prefix="/api/alerts")
    app.include_router(ro.router, prefix="/api/ops")
    return TestClient(app)


def test_replay_without_webhook_says_simulate(api):
    r = api.post("/api/ops/replay_alert/al-1?as_severity=warning").json()
    assert r["sent"] is False and r["simulate"] is True and "N8N_WEBHOOK_URL" in r["error"]
    assert r["payload"]["severity"] == "warning" and r["payload"]["rehearsal"] is True
    assert r["payload"]["true_severity"] == "advisory"
    assert "callback_token" not in r["payload"]
    assert api.post("/api/ops/replay_alert/nope").status_code == 404
    assert api.post("/api/ops/replay_alert/al-1?as_severity=panic").status_code == 422


def test_replay_posts_to_n8n_and_is_audited(api, monkeypatch):
    seen = []
    monkeypatch.setenv("N8N_WEBHOOK_URL", "http://n8n.local/webhook/sentinel-grid/alert")
    monkeypatch.setattr("requests.post", lambda url, json=None, **k: seen.append((url, json)) or FakeResp(200))
    r = api.post("/api/ops/replay_alert/al-1").json()
    assert r["sent"] is True and r["n8n_host"] == "n8n.local"
    url, body = seen[0]
    assert body["alert_id"] == "al-1" and body["demo"] is True and body["callback_base"].startswith("http://testserver")
    ev = api.get("/api/ops/audit?alert_id=al-1").json()["events"]
    assert [e["source"] for e in ev if e["kind"] == "send"] == ["pipeline", "api-replay"]


def test_events_callback_token_and_audit_merge(api, monkeypatch):
    monkeypatch.setenv("OPS_CALLBACK_TOKEN", "s3cret")
    body = {"alert_id": "al-1", "step": "notified_owner", "role": "safety_officer",
            "channel": "https://webhook.site/abc/safety_officer"}
    assert api.post("/api/ops/events", json=body).status_code == 401
    assert api.post("/api/ops/events", json={**body, "step": "launch_missiles"},
                    headers={"X-Ops-Token": "s3cret"}).status_code == 422
    assert api.post("/api/ops/events", json=body, headers={"X-Ops-Token": "s3cret"}).status_code == 201
    api.post("/api/alerts/al-1/ack", json={"acknowledged_by": "Safety officer", "notes": "ok"})
    ev = api.get("/api/ops/audit?alert_id=al-1").json()["events"]
    kinds = [(e["kind"], e.get("step")) for e in ev]
    assert ("route", "notified_owner") in kinds and ("ack", None) in kinds and ("send", None) in kinds
    route = next(e for e in ev if e["kind"] == "route")
    assert route["channel"] == "webhook.site"                                  # host only
    assert all(e["ts"].endswith("+04:00") for e in ev)
    assert api.get("/api/ops/audit?alert_id=al-1&since=2030-01-01T00:00:00Z").json()["count"] == 0


def test_channels_mask_env_urls(api, monkeypatch):
    monkeypatch.setenv("OPS_CH_MEDICAL_LEAD", "https://hooks.slack.com/services/T/B/secret")
    roles = api.get("/api/ops/channels").json()["roles"]
    assert "secret" not in roles["medical_lead"]["url"] and roles["medical_lead"]["url_source"] == "env"
    assert roles["site_supervisor"]["url"].startswith("https://webhook.site/")
