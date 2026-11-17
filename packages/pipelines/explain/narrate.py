"""Plain-English alert narration.

Chunk 5 adds LLM narration. narrate_alert_template() is the deterministic fallback
(and the text served today as ``explanation_template``): built only from fields the
alert record already carries, so it can never state something the detector did not
compute.

    "Heat-stress cluster watch issued for Al Quoz Industrial Area. The alert is driven
     primarily by apparent temperature (54% of signal), with secondary contribution from
     absence rate (31%) and heat-related encounters (15%). All three have been rising
     since 09:00 local time."
"""
from __future__ import annotations

from zoneinfo import ZoneInfo

import pandas as pd

LOCAL_TZ = ZoneInfo("Asia/Dubai")

#: Display names (lower case, mid-sentence). Overridden by driver_details[k]["label"].
SIGNAL_LABELS = {
    "apparent_temp": "apparent temperature",
    "pm25": "PM2.5",
    "pm10": "PM10",
    "absence_rate": "absence rate",
    "respiratory_absence_rate": "respiratory absence rate",
    "heat_related_encounter_count": "heat-related encounters",
    "respiratory_encounter_count": "respiratory encounters",
    "any_encounter_count": "all-cause encounters",
    "fever_encounter_count": "febrile encounters",
}
_COUNT = {1: "It has", 2: "Both have", 3: "All three have", 4: "All four have"}


def _label(alert: dict, sig: str) -> str:
    d = (alert.get("driver_details") or {}).get(sig) or {}
    return d.get("label") or SIGNAL_LABELS.get(sig, sig.replace("_", " "))


def _places(names: list[str]) -> str:
    names = [n for n in names if n]
    if not names:
        return "the affected area"
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{names[0]}, {names[1]} and {len(names) - 2} other site{'s' if len(names) > 3 else ''}"


def _hhmm(ts) -> str | None:
    if not ts:
        return None
    t = pd.Timestamp(ts)
    t = t.tz_localize("UTC") if t.tzinfo is None else t
    return t.tz_convert(LOCAL_TZ).strftime("%H:%M")


def _join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def narrate_alert_template(alert_dict: dict) -> str:
    """One paragraph from severity, scenario, sites and the (SHAP) drivers."""
    a = alert_dict
    sev = a.get("severity", "watch")
    head = f"{a.get('scenario_label', a.get('scenario_id', 'Alert'))} {sev} issued for " \
           f"{_places(list(a.get('site_names') or []))}."
    drivers = {k: float(v) for k, v in (a.get("drivers") or {}).items() if v is not None}
    if not drivers:
        return head
    detail = (a.get("shap") or {}).get("signals") or {}

    def raises(k):
        return (detail.get(k) or {}).get("direction", "raises_risk") == "raises_risk"

    ranked = sorted(drivers.items(), key=lambda kv: -kv[1])
    up = [(k, v) for k, v in ranked if raises(k) and round(v) > 0]
    down = [(k, v) for k, v in ranked if not raises(k) and round(v) > 0]
    out = [head]
    if up:
        k0, v0 = up[0]
        s = f"The alert is driven primarily by {_label(a, k0)} ({v0:.0f}% of signal)"
        rest = [f"{_label(a, k)} ({v:.0f}%)" for k, v in up[1:3]]
        if rest:
            s += f", with secondary contribution from {_join(rest)}"
        out.append(s + ".")
    if down:
        k, v = down[0]
        lab = _label(a, k)
        verb, pulls = ("are", "pull") if lab.endswith("encounters") else ("is", "pulls")
        out.append(f"{lab[0].upper() + lab[1:]} {verb} below {'their' if verb == 'are' else 'its'} "
                   f"usual level and {pulls} the score down ({v:.0f}%).")

    onsets = a.get("driver_onsets") or {}
    top = [k for k, _ in up[:3]]
    have = [k for k in top if onsets.get(k)]
    if top and len(have) == len(top):
        since = _hhmm(max(pd.Timestamp(onsets[k]) for k in have))
        out.append(f"{_COUNT.get(len(have), 'All have')} been rising since {since} local time.")
    elif have:
        k = have[0]
        lab = _label(a, k)
        out.append(f"{lab[0].upper() + lab[1:]} {'have' if lab.endswith('encounters') else 'has'} "
                   f"been above baseline since {_hhmm(onsets[k])} local time.")

    if a.get("drivers_method") == "fusion_weights":
        out.append("(Driver shares are the fusion weights: the explanation model was "
                   "unavailable for this hex.)")
    return " ".join(out)
