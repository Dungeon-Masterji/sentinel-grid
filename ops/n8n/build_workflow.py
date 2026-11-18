"""Generate ops/n8n/workflows/alert_routing.json (the n8n workflow, versioned in git).

    python ops/n8n/build_workflow.py

The workflow routes; it does not decide WHO. Channels, waits and the plan arrive resolved
in the webhook body (packages/pipelines/ops/notify.py, configs/ops_channels.yaml).
Every step reports back to {callback_base}/api/ops/events (header X-Ops-Token =
payload.callback_token; n8n 2.x blocks $env in expressions) so the /ops dashboard can light
up the diagram from real executions; callback failures never stop the routing.

Sticky notes colour-code the canvas by role group (configs/ops_channels.yaml `groups`).
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "ops" / "n8n" / "workflows" / "alert_routing.json"
NS = uuid.UUID("9d6c1c52-0b55-4e43-9a77-3c3a1f6e2b10")
A = "$('Alert').item.json"            # the alert body, flattened by the "Alert" Set node

nodes: list[dict] = []
connections: dict = {}


def nid(name: str) -> str:
    return str(uuid.uuid5(NS, name))


def node(name, type_, version, pos, params, **extra):
    nodes.append({"id": nid(name), "name": name, "type": type_, "typeVersion": version,
                  "position": list(pos), "parameters": params, **extra})
    return name


def connect(src, dst, out=0):
    c = connections.setdefault(src, {"main": []})
    while len(c["main"]) <= out:
        c["main"].append([])
    c["main"][out].append({"node": dst, "type": "main", "index": 0})


def report(name, step, pos, role_expr=None, channel_expr=None):
    """POST a routing step to the API; never blocks routing."""
    body = ("={{ JSON.stringify({ alert_id: " + A + ".alert_id, step: '" + step + "', status: 'done', "
            + "role: " + (role_expr or "null") + ", channel: " + (channel_expr or "null")
            + ", execution_id: String($execution.id) }) }}")
    return node(name, "n8n-nodes-base.httpRequest", 4.2, pos, {
        "method": "POST",
        "url": "={{ " + A + ".callback_base }}/api/ops/events",
        "sendHeaders": True,
        "headerParameters": {"parameters": [{"name": "X-Ops-Token", "value": "={{ " + A + ".callback_token || '' }}"}]},
        "sendBody": True, "specifyBody": "json", "jsonBody": body,
        "options": {"timeout": 5000},
    }, onError="continueRegularOutput", notes=f"Report step '{step}' to /api/ops/events", notesInFlow=False)


def notify(name, who_expr, pos, prefix):
    """POST the message to a role's channel webhook (Slack/Telegram/webhook.site...)."""
    body = ("={{ JSON.stringify({ text: $('" + prefix + "').item.json.message, alert_id: " + A + ".alert_id, "
            "severity: " + A + ".severity, role: " + who_expr + ".role, role_label: " + who_expr + ".label, "
            "action: " + A + ".action, location: " + A + ".location_name, explanation: " + A + ".explanation_en }) }}")
    return node(name, "n8n-nodes-base.httpRequest", 4.2, pos, {
        "method": "POST", "url": "={{ " + who_expr + ".url }}",
        "sendBody": True, "specifyBody": "json", "jsonBody": body,
        "options": {"timeout": 8000},
    }, onError="continueRegularOutput")


def message(name, pos, tag):
    text = ("={{ '[" + tag + "] ' + " + A + ".scenario_label + ' at ' + " + A + ".location_name + "
            "'. ' + (" + A + ".explanation_en || '') + ' ACTION for ' + " + A + ".channels.owner.label + ': ' + "
            + A + ".action.specific_action + ' (' + " + A + ".action.time_window + '). Alert ' + " + A + ".alert_id }}")
    return node(name, "n8n-nodes-base.set", 3.4, pos, {
        "mode": "manual",
        "assignments": {"assignments": [{"id": nid(name + "m"), "name": "message", "type": "string", "value": text}]},
        "options": {},
    })


def wait(name, seconds_expr, pos):
    return node(name, "n8n-nodes-base.wait", 1.1, pos, {
        "resume": "timeInterval", "amount": "={{ " + seconds_expr + " }}", "unit": "seconds"})


def check_ack(name, pos):
    return node(name, "n8n-nodes-base.httpRequest", 4.2, pos, {
        "method": "GET", "url": "={{ " + A + ".callback_base }}/api/alerts/{{ " + A + ".alert_id }}",
        "options": {"timeout": 8000}}, onError="continueRegularOutput")


def acked_switch(name, check_node, pos):
    """0 = a human (anyone but n8n itself) acknowledged AFTER this routing run was sent,
    1 = not acknowledged. Older acks of the same alert do not count."""
    expr = ("={{ (($('" + check_node + "').item.json.alert || {}).acks || [])"
            ".some(a => !String(a.acknowledged_by || '').startsWith('n8n')"
            " && new Date(a.acknowledged_at) >= new Date(" + A + ".sent_at)) ? 0 : 1 }}")
    return node(name, "n8n-nodes-base.switch", 3.2, pos, {
        "mode": "expression", "numberOutputs": 2, "output": expr, "options": {}})


def sticky(name, content, pos, w, h, color):
    node(name, "n8n-nodes-base.stickyNote", 1, pos, {"content": content, "width": w, "height": h, "color": color})


def build() -> dict:
    cfg = yaml.safe_load((ROOT / "configs" / "ops_channels.yaml").read_text())
    g = cfg["groups"]
    roles_by_group = {k: [r["label"] for r in cfg["roles"].values() if r["group"] == k] for k in g}

    # ---- canvas notes (colour = role group) -----------------------------------------
    sticky("Sentinel Grid · alert routing",
           "## Sentinel Grid · alert routing\nPOST /webhook/sentinel-grid/alert from the alert pipeline or "
           "`POST /api/ops/replay_alert/{id}`.\nChannels, waits and plan arrive **resolved** in the body "
           "(configs/ops_channels.yaml). Each step reports to `/api/ops/events` → live /ops diagram.",
           (-120, -420), 560, 220, 7)
    sticky("Watch · log only", "### WATCH · log only\nAuto-acknowledged as `n8n-auto-log`.", (700, -260), 480, 220, 7)
    sticky("Owner channels",
           "### Owner (advisory)\nColour per role group:\n" + "\n".join(
               f"- **{g[k]['label']}**: {', '.join(roles_by_group[k])}" for k in g),
           (700, 0), 1460, 260, g["site"]["n8n_sticky"])
    sticky("Escalation",
           "### WARNING · owner + escalation contact\n(MoHRE inspector, DoH surveillance, site supervisor... "
           "per `escalates_to`)", (700, 320), 1460, 260, g["regulator"]["n8n_sticky"])
    sticky("Emirate operations lead", "### Final escalation\nEmirate operations lead",
           (2200, 320), 520, 260, g["emirate"]["n8n_sticky"])

    # ---- trigger --------------------------------------------------------------------
    node("Alert in (webhook)", "n8n-nodes-base.webhook", 2, (-100, 100), {
        "httpMethod": "POST", "path": "sentinel-grid/alert", "responseMode": "onReceived", "options": {}},
        webhookId=nid("webhook"))
    node("Alert", "n8n-nodes-base.set", 3.4, (120, 100), {
        "mode": "raw", "jsonOutput": "={{ $json.body }}", "options": {}})
    report("Report: received", "received", (320, 100))
    report("Report: routed", "routed", (500, 100))
    node("Severity router", "n8n-nodes-base.switch", 3.2, (680, 100), {
        "mode": "expression", "numberOutputs": 3,
        "output": "={{ ({watch: 0, advisory: 1, warning: 2})[" + A + ".severity] ?? 0 }}", "options": {}},
        notes="0 watch · 1 advisory · 2 warning", notesInFlow=True)
    connect("Alert in (webhook)", "Alert")
    connect("Alert", "Report: received")
    connect("Report: received", "Report: routed")
    connect("Report: routed", "Severity router")

    # ---- watch ------------------------------------------------------------------------
    node("Watch: auto-log ack", "n8n-nodes-base.httpRequest", 4.2, (760, -160), {
        "method": "POST", "url": "={{ " + A + ".callback_base }}/api/alerts/{{ " + A + ".alert_id }}/ack",
        "sendBody": True, "specifyBody": "json",
        "jsonBody": "={{ JSON.stringify({ acknowledged_by: 'n8n-auto-log', notes: 'severity=watch, recorded only' }) }}",
        "options": {"timeout": 8000}}, onError="continueRegularOutput")
    report("Report: logged", "logged", (980, -160))
    connect("Severity router", "Watch: auto-log ack", 0)
    connect("Watch: auto-log ack", "Report: logged")

    # ---- advisory ---------------------------------------------------------------------
    message("Advisory message", (760, 100), "ADVISORY")
    notify("Notify owner", A + ".channels.owner", (960, 100), "Advisory message")
    report("Report: owner notified", "notified_owner", (1160, 100), A + ".channels.owner.role", A + ".channels.owner.url")
    wait("Wait for ack (15 min)", A + ".waits.ack_wait_s", (1360, 100))
    check_ack("Check ack", (1560, 100))
    acked_switch("Acknowledged?", "Check ack", (1760, 100))
    report("Report: acked", "acked", (1980, 40))
    connect("Severity router", "Advisory message", 1)
    connect("Advisory message", "Notify owner")
    connect("Notify owner", "Report: owner notified")
    connect("Report: owner notified", "Wait for ack (15 min)")
    connect("Wait for ack (15 min)", "Check ack")
    connect("Check ack", "Acknowledged?")
    connect("Acknowledged?", "Report: acked", 0)

    # ---- warning / escalation -------------------------------------------------------------
    message("Escalation message", (760, 420), "ESCALATION")
    notify("Notify owner (escalation)", A + ".channels.owner", (960, 360), "Escalation message")
    notify("Notify escalation contact", A + ".channels.supervisor", (960, 480), "Escalation message")
    node("Both notified", "n8n-nodes-base.merge", 3, (1160, 420), {
        "mode": "chooseBranch", "useDataOfInput": 2})
    report("Report: escalated", "escalated", (1360, 420), A + ".channels.supervisor.role", A + ".channels.supervisor.url")
    wait("Wait for ack (escalation)", A + ".waits.escalation_wait_s", (1560, 420))
    check_ack("Check ack (escalation)", (1760, 420))
    acked_switch("Acknowledged after escalation?", "Check ack (escalation)", (1960, 420))
    report("Report: acked after escalation", "acked_after_escalation", (2240, 360))
    message("Final escalation message", (2240, 480), "FINAL ESCALATION")
    notify("Notify emirate operations lead", A + ".channels.emirate_ops_lead", (2440, 480), "Final escalation message")
    report("Report: emirate ops lead", "emirate_ops_lead", (2640, 480),
           A + ".channels.emirate_ops_lead.role", A + ".channels.emirate_ops_lead.url")
    connect("Severity router", "Escalation message", 2)
    connect("Acknowledged?", "Escalation message", 1)            # advisory not acked -> escalate
    connect("Escalation message", "Notify owner (escalation)")
    connect("Escalation message", "Notify escalation contact")
    connections.setdefault("Notify owner (escalation)", {"main": [[]]})["main"][0].append(
        {"node": "Both notified", "type": "main", "index": 0})
    connections.setdefault("Notify escalation contact", {"main": [[]]})["main"][0].append(
        {"node": "Both notified", "type": "main", "index": 1})
    connect("Both notified", "Report: escalated")
    connect("Report: escalated", "Wait for ack (escalation)")
    connect("Wait for ack (escalation)", "Check ack (escalation)")
    connect("Check ack (escalation)", "Acknowledged after escalation?")
    connect("Acknowledged after escalation?", "Report: acked after escalation", 0)
    connect("Acknowledged after escalation?", "Final escalation message", 1)
    connect("Final escalation message", "Notify emirate operations lead")
    connect("Notify emirate operations lead", "Report: emirate ops lead")

    return {"name": "Sentinel Grid · alert routing", "nodes": nodes, "connections": connections,
            "settings": {"executionOrder": "v1", "saveManualExecutions": True,
                         "saveDataSuccessExecution": "all"},
            "active": False, "pinData": {},
            "meta": {"templateCredsSetupCompleted": True},
            "tags": [], "id": "SentinelAlertRouting"}


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(build(), indent=2) + "\n")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(nodes)} nodes)")
