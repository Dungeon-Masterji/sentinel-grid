"""LLM narration, Arabic translation and structured actions for alerts (Groq).

Per alert (enrich_alert):
  1. narrate:    build_alert_narration_prompt -> GROQ_MODEL -> validated 3-sentence English
  2. translate:  translate_to_arabic(english) -> GROQ_MODEL_AR -> Arabic, numbers verbatim
  3. action:     recommend_action -> GROQ_MODEL, JSON mode -> pydantic RecommendedAction

Every step has a deterministic fallback (narrate_alert_template, its Arabic counterpart,
the scenario's recommended_actions[severity]) and records which one was used:
explanation_source / explanation_ar_source / action.source in {"llm", "template_fallback"}.

Hallucination guards: decimals and percentages in the English text must appear in the
facts given to the model; every number in the English must reappear in the Arabic; the
action must parse as RecommendedAction with an owner from the scenario's valid roles.

Budgets: at most per_alert_token_budget completion tokens per alert across all three calls
(cached results cost nothing), and no calls once today's total spend (UTC) passes
daily_token_budget. A missing key, exhausted budget or repeated 429s switch the LLM off
for the rest of the run, so a run never stalls alert-by-alert on retries.

Caching (groq_client.NarrationCache): English per (alert_id, prompt hash of severity +
driver ranking + owner), Arabic per (alert_id, hash of the English), actions per
scenario + hex + severity for action_cache_hours (6, the dedupe window).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, field_validator

from .groq_client import (LLMOffline, LLMReply, LLMUnavailable, NarrationCache, chat,
                          default_cache, model_ar, model_en, prompt_hash)
from .narrate import narrate_alert_template, narrate_alert_template_ar
from .prompts import (PROMPT_VERSION, SEVERITY_ESCALATION_MIN, SEVERITY_WINDOWS, alert_facts, build_action_prompt,
                      build_alert_narration_prompt, build_translation_prompt,
                      default_owner_role, format_facts, valid_owner_roles)

log = logging.getLogger("sentinel.explain.llm")

NARRATION_DEFAULTS = {
    "daily_token_budget": 100_000,
    "per_alert_token_budget": 500,
    "action_cache_hours": 6,
    "fallback_time_window": {"watch": "within_4h", "advisory": "within_1h", "warning": "immediate"},
    "fallback_escalation_minutes": {"watch": 240, "advisory": 60, "warning": 15},  # within SEVERITY_ESCALATION_MIN
}
MIN_CALL_TOKENS = 80          # don't start a call with less completion budget than this
MAX_TOKENS = {"narration": 400, "arabic": 300, "action": 250}


def narration_config(cfg: dict | None) -> dict:
    return {**NARRATION_DEFAULTS, **((cfg or {}).get("narration") or {})}


# ---------------------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------------------
OwnerRole = Literal["site_supervisor", "safety_officer", "medical_lead", "operations_manager", "hr_lead"]
TimeWindow = Literal["immediate", "within_1h", "within_4h", "within_24h", "this_week"]


class RecommendedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner_role: OwnerRole
    time_window: TimeWindow
    specific_action: str = Field(min_length=8, max_length=300)
    escalation_if_no_ack_in_minutes: StrictInt = Field(gt=0, le=7 * 24 * 60)

    @field_validator("specific_action")
    @classmethod
    def _one_sentence(cls, v: str) -> str:
        v = " ".join(v.split())
        if len(re.findall(r"[.!?](?=\s|$)", v)) > 1:
            raise ValueError("specific_action must be one sentence")
        return v


# ---------------------------------------------------------------------------------------
# per-run state
# ---------------------------------------------------------------------------------------
class AlertBudget:
    """Completion tokens left for one alert."""

    def __init__(self, tokens: int):
        self.left = int(tokens)
        self.used = 0

    def cap(self, want: int) -> int:
        n = min(int(want), self.left)
        if n < MIN_CALL_TOKENS:
            raise LLMUnavailable(f"per-alert token budget spent ({self.used} used)")
        return n

    def spend(self, reply: LLMReply) -> None:
        if not reply.cached:
            self.left -= reply.completion_tokens
            self.used += reply.total_tokens


class RunState:
    def __init__(self, ncfg: dict, cache: NarrationCache):
        self.ncfg = ncfg
        self.cache = cache
        self.offline: str | None = None
        self.calls = 0
        self.tokens = 0

    def call(self, messages, *, budget: AlertBudget, kind: str, **kw) -> LLMReply:
        if self.offline:
            raise LLMOffline(self.offline)
        try:
            r = chat(messages, max_tokens=budget.cap(MAX_TOKENS[kind]), cache=self.cache,
                     daily_budget=int(self.ncfg["daily_token_budget"]), **kw)
        except LLMOffline as e:
            self.offline = str(e)
            raise
        self.calls += 1
        self.tokens += r.total_tokens
        budget.spend(r)
        return r


# ---------------------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------------------
_NUM = re.compile(r"\d+(?:\.\d+)?")
_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫٪", "01234567890123456789.%")
_ARABIC = re.compile(r"[؀-ۿ]")


def clean_text(t: str) -> str:
    t = re.sub(r"[*_`#]+", "", t)
    t = re.sub(r"^\s*(?:\(?\d\)|\d\.)\s+", "", t, flags=re.M)     # "(1) " / "1. " prefixes
    return " ".join(t.split())


def validate_narration(text: str, facts_text: str) -> str:
    t = clean_text(text)
    if not 40 <= len(t) <= 900:
        raise ValueError(f"narration length {len(t)} out of range")
    n = len([s for s in re.split(r"(?<=[.!?])\s+", t) if s.strip()])
    if not 2 <= n <= 4:
        raise ValueError(f"narration has {n} sentences, expected 3")
    allowed = set(_NUM.findall(facts_text))
    for m in re.finditer(r"(\d+(?:\.\d+)?)(\s?%)?", t):
        num, pct = m.group(1), m.group(2)
        if ("." in num or pct) and num not in allowed:
            raise ValueError(f"number {m.group(0)!r} is not in the facts")
    return t


def normalise_arabic(t: str) -> str:
    return " ".join(t.translate(_AR_DIGITS).split())


def validate_arabic(arabic: str, english: str) -> str:
    a = normalise_arabic(arabic)
    if len(_ARABIC.findall(a)) < 20:
        raise ValueError("translation is not Arabic")
    have = set(_NUM.findall(a))
    missing = [n for n in _NUM.findall(english) if n not in have]
    if missing:
        raise ValueError(f"numbers missing from Arabic: {missing}")
    return a


def parse_action(text: str, scenario: dict, severity: str | None = None) -> RecommendedAction:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    m = re.search(r"\{.*\}", t, flags=re.S)
    if not m:
        raise ValueError("no JSON object in reply")
    act = RecommendedAction.model_validate(json.loads(m.group(0)))
    if act.owner_role not in valid_owner_roles(scenario):
        raise ValueError(f"owner_role {act.owner_role} not valid for {scenario.get('id')}")
    if severity in SEVERITY_WINDOWS and act.time_window not in SEVERITY_WINDOWS[severity]:
        raise ValueError(f"time_window {act.time_window} too urgent/lax for a {severity}")
    lo, hi = SEVERITY_ESCALATION_MIN.get(severity, (1, 7 * 24 * 60))
    if not lo <= act.escalation_if_no_ack_in_minutes <= hi:
        raise ValueError(f"escalation {act.escalation_if_no_ack_in_minutes} min outside {lo}-{hi} for a {severity}")
    return act


# ---------------------------------------------------------------------------------------
# the three steps
# ---------------------------------------------------------------------------------------
def _state(cache: NarrationCache | None, ncfg: dict | None, state: RunState | None) -> RunState:
    return state or RunState(ncfg or narration_config(None), cache or default_cache())


def narration_cache_key(alert: dict, scenario: dict) -> dict:
    """What the English text depends on. Readings drift between 15-minute runs; severity,
    the driver ranking and the owner are what would change the message."""
    f = alert_facts(alert, scenario)
    return {"v": PROMPT_VERSION, "scenario": scenario.get("id"), "severity": f["severity"],
            "sites": f["sites"], "drivers": [d["signal"] for d in f["drivers"][:3]],
            "owner": f["owner"]}


def narrate_alert(alert: dict, scenario: dict, *, budget: AlertBudget, state: RunState,
                  force: bool = False) -> tuple[str, LLMReply]:
    """Validated English narration; raises LLMUnavailable/ValueError on failure."""
    msgs = build_alert_narration_prompt(alert, scenario)
    model = model_en()
    phash = prompt_hash({"model": model, "key": narration_cache_key(alert, scenario)})
    if not force:
        hit = state.cache.get(alert["id"], "narration_en", phash)
        if hit is not None:
            return hit, LLMReply(text=hit, model=model, cached=True)
    facts_text = format_facts(alert_facts(alert, scenario))
    err: Exception | None = None
    for temperature in (0.3, 0.1):
        r = state.call(msgs, budget=budget, kind="narration", model=model, temperature=temperature)
        try:
            text = validate_narration(r.text, facts_text)
        except ValueError as e:
            err = e
            log.info("%s: narration rejected (%s): %r", alert["id"], e, r.text[:160])
            continue
        state.cache.put(alert["id"], "narration_en", phash, text, r.model)
        return text, r
    raise ValueError(f"narration rejected twice: {err}")


def translate_to_arabic(english_text: str, *, alert_id: str | None = None,
                        budget: AlertBudget | None = None, state: RunState | None = None,
                        cache: NarrationCache | None = None, force: bool = False) -> str:
    """Modern Standard Arabic via GROQ_MODEL_AR, numbers kept verbatim (Western digits).
    Retries once with a stricter prompt; raises LLMUnavailable/ValueError on failure."""
    state = _state(cache, None, state)
    budget = budget or AlertBudget(MAX_TOKENS["arabic"] * 2)
    model = model_ar()
    phash = prompt_hash({"model": model, "en": english_text})
    if alert_id and not force:
        hit = state.cache.get(alert_id, "narration_ar", phash)
        if hit is not None:
            return hit
    err: Exception | None = None
    for strict in (False, True):
        r = state.call(build_translation_prompt(english_text, strict=strict), budget=budget,
                       kind="arabic", model=model, temperature=0.2 if not strict else 0.0)
        try:
            ar = validate_arabic(r.text, english_text)
        except ValueError as e:
            err = e
            log.info("Arabic rejected (%s): %r", e, r.text[:120])
            continue
        if alert_id:
            state.cache.put(alert_id, "narration_ar", phash, ar, r.model)
        return ar
    raise ValueError(f"translation rejected twice: {err}")


def fallback_action(alert: dict, scenario: dict, ncfg: dict | None = None) -> dict:
    ncfg = ncfg or narration_config(None)
    sev = alert.get("severity", "watch")
    acts = (scenario.get("recommended_actions") or {}).get(sev) or ["Review the alert and confirm site conditions."]
    a = RecommendedAction(
        owner_role=default_owner_role(scenario, sev),
        time_window=ncfg["fallback_time_window"][sev],
        specific_action=acts[0],
        escalation_if_no_ack_in_minutes=int(ncfg["fallback_escalation_minutes"][sev]))
    return {**a.model_dump(), "source": "template_fallback", "model": None}


def recommend_action(alert_dict: dict, scenario_config: dict, *, narrative: str | None = None,
                     budget: AlertBudget | None = None, state: RunState | None = None,
                     cache: NarrationCache | None = None, ncfg: dict | None = None,
                     force: bool = False) -> dict:
    """Structured action dict (RecommendedAction fields + source + model). Never raises:
    malformed output is retried once with a tightened prompt, then the scenario's
    recommended_actions[severity] is used."""
    ncfg = ncfg or narration_config(None)
    state = _state(cache, ncfg, state)
    budget = budget or AlertBudget(MAX_TOKENS["action"] * 2)
    narrative = narrative or alert_dict.get("explanation_en")
    key = f"{PROMPT_VERSION}|{alert_dict['scenario_id']}|{alert_dict['h3_cell']}|{alert_dict.get('severity')}"
    if not force:
        hit = state.cache.get_action(key, float(ncfg["action_cache_hours"]))
        if hit is not None:
            return hit
    model = model_en()
    for tightened in (False, True):
        try:
            r = state.call(build_action_prompt(alert_dict, scenario_config, narrative, tightened),
                           budget=budget, kind="action", model=model, temperature=0.2,
                           json_mode=True)
            act = parse_action(r.text, scenario_config, alert_dict.get("severity"))
        except (ValueError, ValidationError) as e:   # JSONDecodeError is a ValueError
            log.info("%s: action rejected (%s)", alert_dict.get("id"), str(e)[:200])
            continue
        except LLMUnavailable as e:
            log.info("%s: action LLM unavailable (%s)", alert_dict.get("id"), e)
            break
        out = {**act.model_dump(), "source": "llm", "model": r.model}
        state.cache.put_action(key, out)
        return out
    return fallback_action(alert_dict, scenario_config, ncfg)


# ---------------------------------------------------------------------------------------
# per alert / per run
# ---------------------------------------------------------------------------------------
def enrich_alert(alert: dict, scenario: dict, *, state: RunState, force: bool = False) -> dict:
    """Set explanation_en / explanation_ar / explanation_source / explanation_ar_source /
    action / narration on ``alert`` (in place) and return it."""
    ncfg = state.ncfg
    budget = AlertBudget(int(ncfg["per_alert_token_budget"]))
    meta = {"model": None, "model_ar": None, "errors": []}

    try:
        en, r = narrate_alert(alert, scenario, budget=budget, state=state, force=force)
        alert["explanation_en"], alert["explanation_source"] = en, "llm"
        meta["model"], meta["cached"] = r.model, r.cached
    except (LLMUnavailable, ValueError) as e:
        alert["explanation_en"] = narrate_alert_template(alert)
        alert["explanation_source"] = "template_fallback"
        meta["errors"].append(f"narration: {e}"[:200])

    ar = None
    if alert["explanation_source"] == "llm":
        try:
            ar = translate_to_arabic(alert["explanation_en"], alert_id=alert["id"], budget=budget,
                                     state=state, force=force)
            meta["model_ar"] = model_ar()
        except (LLMUnavailable, ValueError) as e:
            meta["errors"].append(f"arabic: {e}"[:200])
    alert["explanation_ar"] = ar or narrate_alert_template_ar(alert)
    alert["explanation_ar_source"] = "llm" if ar else "template_fallback"

    alert["action"] = recommend_action(alert, scenario, narrative=alert["explanation_en"],
                                       budget=budget, state=state, ncfg=ncfg, force=force)
    meta["completion_budget_left"] = budget.left
    meta["tokens"] = budget.used
    meta["generated_at"] = state.cache.now().astimezone(timezone.utc).isoformat(timespec="seconds")
    if state.offline:
        meta["offline"] = state.offline[:200]
    alert["narration"] = meta
    return alert


def narrate_alerts(alerts: list[dict], cfg: dict, *, cache: NarrationCache | None = None,
                   enabled: bool = True) -> dict:
    """Enrich every alert in place. With enabled=False (or no GROQ_API_KEY) everything is
    template_fallback. Returns run stats for meta.json."""
    ncfg = narration_config(cfg)
    state = RunState(ncfg, cache or default_cache())
    if not enabled:
        state.offline = "LLM narration disabled"
    scen = {s["id"]: s for s in cfg["scenarios"]}
    for a in alerts:
        enrich_alert(a, scen[a["scenario_id"]], state=state)
    try:
        state.cache.prune()
    except Exception as e:  # noqa: BLE001 - housekeeping must not fail a run
        log.warning("narration cache prune failed: %s", e)
    stats = {"alerts": len(alerts), "calls": state.calls, "tokens": state.tokens,
             "tokens_today": state.cache.tokens_today(), "offline": state.offline,
             "llm_en": sum(a["explanation_source"] == "llm" for a in alerts),
             "llm_ar": sum(a["explanation_ar_source"] == "llm" for a in alerts),
             "llm_action": sum(a["action"]["source"] == "llm" for a in alerts)}
    log.info("narration: %s", stats)
    return stats
