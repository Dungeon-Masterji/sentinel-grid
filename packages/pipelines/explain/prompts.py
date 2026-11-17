"""Prompt templates for LLM alert narration, Arabic translation and structured actions.

Everything the model sees is built by alert_facts() from the alert record: scenario,
severity, site names (places, never people), the SHAP drivers (alert["drivers"], not
the fusion-weight prior), current readings against baseline, the action owner as a
role title, and the local time. Clinical inputs are aggregate catchment counts only;
no patient-level field exists in an alert, so none can reach a prompt.

Few-shot examples live in fixtures/fewshot.json (2 per scenario). They are stored as
facts and rendered with the same format_facts() as live alerts, so examples and real
prompts never drift apart.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

PROMPT_VERSION = "v2"
LOCAL_TZ = ZoneInfo("Asia/Dubai")
FEWSHOT_PATH = Path(__file__).resolve().parent / "fixtures" / "fewshot.json"

SYSTEM_NARRATION = (
    "You are a UAE public-health operations assistant. You communicate crisply with site "
    "supervisors, government inspectors, and clinicians. You never give medical diagnoses. "
    "You stay factual, avoid alarm, and recommend exactly one specific action. You respect "
    "religious, cultural, and labor-law context of the UAE. Output must be exactly: (1) one "
    "sentence stating the alert, (2) one sentence naming the top driver and why it matters, "
    "(3) one sentence with the specific recommended action for the named owner."
)
NARRATION_RULES = (
    "Use only the facts given. Quote percentages and readings exactly as written; do not "
    "invent numbers, people, names or causes. Refer to people by role only. Plain text, "
    "no markdown, no lists, no preamble."
)
SYSTEM_TRANSLATE = (
    "Translate the following UAE public-health alert to Modern Standard Arabic suitable for "
    "a government operations center. Keep technical terms where they are English "
    "conventions. Return only the Arabic text."
)
TRANSLATE_STRICT = (
    " Copy every number, decimal and percentage exactly as written, using Western digits "
    "(0-9) and the % sign. Keep site names, PM2.5, PM10 and units such as °C and µg/m³ in "
    "their original form."
)

OWNER_ROLES = ("site_supervisor", "safety_officer", "medical_lead", "operations_manager", "hr_lead")
TIME_WINDOWS = ("immediate", "within_1h", "within_4h", "within_24h", "this_week")
#: Urgency allowed per severity (avoid alarm: a watch is never "immediate").
SEVERITY_WINDOWS = {"watch": ("within_4h", "within_24h"),
                    "advisory": ("within_1h", "within_4h"),
                    "warning": ("immediate", "within_1h")}
SEVERITY_ESCALATION_MIN = {"watch": (60, 1440), "advisory": (30, 240), "warning": (5, 60)}
WINDOW_TEXT = {"immediate": "immediately", "within_1h": "within 1 hour",
               "within_4h": "within 4 hours", "within_24h": "within 24 hours",
               "this_week": "this week"}


def role_title(role: str) -> str:
    return role.replace("_", " ").replace("hr ", "HR ")


#: Signal display names for prompts (more explicit than the chart labels).
SIGNAL_NAMES = {
    "apparent_temp": "apparent temperature",
    "pm25": "PM2.5",
    "pm10": "PM10",
    "absence_rate": "worker absence rate",
    "respiratory_absence_rate": "respiratory-coded worker absence rate",
    "heat_related_encounter_count": "heat-related clinic encounters in the catchment",
    "respiratory_encounter_count": "respiratory clinic encounters in the catchment",
    "any_encounter_count": "all-cause clinic encounters in the catchment",
    "fever_encounter_count": "febrile clinic encounters in the catchment",
}


def _period(hour: int) -> str:
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 21:
        return "evening"
    return "night"


def _fmt_value(sig: str, unit: str | None, v: float | None) -> str | None:
    if v is None:
        return None
    if unit == "rate":
        return f"{100 * v:.1f}%"
    if unit and unit.startswith("encounters"):
        return f"{v:.0f} per 24 h" if float(v).is_integer() else f"{v:.1f} per 24 h"
    return f"{v:.1f} {unit}" if unit else f"{v:.1f}"


def default_owner_role(scenario: dict, severity: str) -> str:
    roles = scenario.get("owner_roles") or {}
    return (roles.get("default") or {}).get(severity) or (roles.get("valid") or ["operations_manager"])[0]


def valid_owner_roles(scenario: dict) -> list[str]:
    return list((scenario.get("owner_roles") or {}).get("valid") or OWNER_ROLES)


def alert_facts(alert: dict, scenario: dict) -> dict:
    """The only alert content any prompt sees. JSON-serialisable."""
    import pandas as pd

    sev = alert.get("severity", "watch")
    details = alert.get("driver_details") or {}
    shap_sig = ((alert.get("shap") or {}).get("signals") or {})
    drivers = []
    for k, v in sorted((alert.get("drivers") or {}).items(), key=lambda kv: -float(kv[1] or 0)):
        if v is None or round(float(v)) <= 0:
            continue
        direction = (shap_sig.get(k) or {}).get("direction", "raises_risk")
        drivers.append({"signal": SIGNAL_NAMES.get(k, k.replace("_", " ")),
                        "pct": int(round(float(v))),
                        "effect": "raises risk" if direction == "raises_risk" else "lowers risk"})
    readings = []
    for k, d in details.items():
        if d.get("suppressed") or d.get("value") is None:
            continue
        unit = d.get("unit")
        readings.append({"signal": SIGNAL_NAMES.get(k, k.replace("_", " ")),
                         "now": _fmt_value(k, unit, d.get("value")),
                         "usual": _fmt_value(k, unit, d.get("baseline"))})
    ts = alert.get("peak_at") or alert.get("as_of")
    local = None
    if ts:
        t = pd.Timestamp(ts)
        t = t.tz_localize("UTC") if t.tzinfo is None else t
        local = t.tz_convert(LOCAL_TZ)
    owner = alert.get("owner_role") or default_owner_role(scenario, sev)
    return {
        "scenario": alert.get("scenario_label") or scenario.get("label"),
        "severity": sev,
        "sites": [s for s in (alert.get("site_names") or []) if s],
        "emirates": list(alert.get("emirates") or []),
        "local_time": f"{local:%H:%M} ({_period(local.hour)})" if local is not None else None,
        "drivers": drivers,
        "readings": readings,
        "owner": role_title(owner),
        "response_window": " to ".join(WINDOW_TEXT[w] for w in SEVERITY_WINDOWS.get(sev, ())),
        "approved_actions": list((scenario.get("recommended_actions") or {}).get(sev) or []),
    }


def format_facts(f: dict) -> str:
    lines = [
        f"Scenario: {f['scenario']}",
        f"Severity: {f['severity']} (scale: watch < advisory < warning)",
        f"Location: {', '.join(f['sites']) or 'the affected area'}"
        + (f" ({', '.join(f['emirates'])})" if f.get("emirates") else ""),
    ]
    if f.get("local_time"):
        lines.append(f"Time of day: {f['local_time']}, Asia/Dubai")
    lines.append("Drivers (SHAP share of this alert's score):")
    lines += [f"- {d['signal']}: {d['pct']}% ({d['effect']})" for d in f["drivers"]] or ["- none available"]
    if f.get("readings"):
        lines.append("Current readings (usual level for this hour in brackets):")
        lines += [f"- {r['signal']}: {r['now']}" + (f" (usual {r['usual']})" if r.get("usual") else "")
                  for r in f["readings"]]
    lines.append(f"Action owner: {f['owner']}")
    if f.get("response_window"):
        lines.append(f"Expected response time for this severity: {f['response_window']}")
    if f.get("approved_actions"):
        lines.append("Approved actions for this severity (pick or adapt one):")
        lines += [f"- {a}" for a in f["approved_actions"]]
    return "\n".join(lines)


@lru_cache(maxsize=1)
def _fewshot() -> dict:
    return json.loads(FEWSHOT_PATH.read_text(encoding="utf-8"))


def fewshot_examples(scenario_id: str) -> list[dict]:
    return list(_fewshot().get(scenario_id, []))


def build_alert_narration_prompt(alert_dict: dict, scenario_config: dict) -> list[dict]:
    """[system, (user, assistant) x 2 few-shot, user] for one alert."""
    msgs = [{"role": "system", "content": f"{SYSTEM_NARRATION}\n{NARRATION_RULES}"}]
    for ex in fewshot_examples(scenario_config.get("id") or alert_dict.get("scenario_id", "")):
        msgs.append({"role": "user", "content": format_facts(ex["facts"])})
        msgs.append({"role": "assistant", "content": ex["response"]})
    msgs.append({"role": "user", "content": format_facts(alert_facts(alert_dict, scenario_config))})
    return msgs


def build_translation_prompt(english_text: str, strict: bool = False) -> list[dict]:
    return [{"role": "system", "content": SYSTEM_TRANSLATE + (TRANSLATE_STRICT if strict else "")},
            {"role": "user", "content": english_text}]


def build_action_prompt(alert_dict: dict, scenario_config: dict, narrative: str | None,
                        tightened: bool = False) -> list[dict]:
    valid = valid_owner_roles(scenario_config)
    sev = alert_dict.get("severity", "watch")
    windows = list(SEVERITY_WINDOWS.get(sev, TIME_WINDOWS))
    lo, hi = SEVERITY_ESCALATION_MIN.get(sev, (5, 1440))
    system = (
        "You turn a UAE public-health alert into ONE structured operational action. You never "
        "give medical diagnoses and never name people, only roles. Reply with a single JSON "
        "object and nothing else, with exactly these keys:\n"
        f'  "owner_role": one of {json.dumps(valid)}\n'
        f'  "time_window": one of {json.dumps(windows)} (matched to a {sev})\n'
        '  "specific_action": one imperative sentence (max 30 words) the owner can do now\n'
        f'  "escalation_if_no_ack_in_minutes": an integer from {lo} to {hi}\n'
        "specific_action must not contradict time_window (no \"immediately\" unless it is "
        "immediate). owner_role is normally the action owner named in the facts. Prefer the approved "
        "actions; the narrative's recommendation and your action must agree."
    )
    if tightened:
        system += ("\nYour previous reply was rejected. Output ONLY the JSON object: no prose, no "
                   "code fences, no extra keys, owner_role and time_window copied exactly from the "
                   "allowed values, escalation_if_no_ack_in_minutes as a bare integer.")
    user = format_facts(alert_facts(alert_dict, scenario_config))
    if narrative:
        user += f"\n\nAlert narrative:\n{narrative}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
