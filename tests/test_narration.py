"""LLM narration (packages/pipelines/explain/llm.py): prompts, validation, fallbacks,
caching and budgets. Groq is always mocked; nothing here touches the network."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from packages.pipelines.explain import groq_client as G
from packages.pipelines.explain import llm as L
from packages.pipelines.explain import prompts as P
from packages.pipelines.fusion.config import SCENARIOS_YAML, load_scenarios

CFG = load_scenarios(SCENARIOS_YAML)
SCEN = {s["id"]: s for s in CFG["scenarios"]}
HEAT = SCEN["heat-stress-cluster"]


def heat_alert(**kw) -> dict:
    a = {
        "id": "a-1", "scenario_id": "heat-stress-cluster", "scenario_label": "Heat-stress cluster",
        "severity": "advisory", "h3_cell": "873f5a000ffffff",
        "site_names": ["Al Quoz Industrial Area"], "emirates": ["Dubai"],
        "peak_at": "2026-08-20T09:00:00Z",       # 13:00 Dubai
        # SHAP shares deliberately different from the fusion-weight prior
        "drivers": {"apparent_temp": 54.0, "absence_rate": 31.0, "heat_related_encounter_count": 15.0},
        "drivers_prior": {"apparent_temp": 45.0, "absence_rate": 35.0, "heat_related_encounter_count": 20.0},
        "drivers_method": "shap",
        "shap": {"signals": {"apparent_temp": {"direction": "raises_risk"},
                             "absence_rate": {"direction": "raises_risk"},
                             "heat_related_encounter_count": {"direction": "lowers_risk"}}},
        "driver_details": {
            "apparent_temp": {"pillar": "env", "unit": "°C", "value": 46.2, "baseline": 39.1, "suppressed": False},
            "absence_rate": {"pillar": "occ", "unit": "rate", "value": 0.084, "baseline": 0.04, "suppressed": False},
            "heat_related_encounter_count": {"pillar": "clin", "unit": "encounters/24h", "value": 6.0,
                                             "baseline": 2.0, "suppressed": False},
        },
        "recommended_actions": HEAT["recommended_actions"]["advisory"],
    }
    a.update(kw)
    return a


GOOD_EN = ("A heat-stress cluster advisory is active for Al Quoz Industrial Area in Dubai this afternoon. "
           "Apparent temperature drives 54% of the signal at 46.2 °C against a usual 39.1 °C, which raises "
           "the risk of heat illness for outdoor crews. The safety officer should move heavy outdoor tasks "
           "to before 10:00 or after 16:00 within the next hour.")
GOOD_AR = ("تم إصدار تنبيه بشأن تجمّع حالات الإجهاد الحراري في منطقة القوز الصناعية في دبي بعد الظهر. "
           "تشكل درجة الحرارة المحسوسة 54% من الإشارة عند 46.2 °C مقابل 39.1 °C المعتادة. "
           "يجب على ضابط السلامة نقل المهام الثقيلة إلى ما قبل 10:00 أو بعد 16:00.")
GOOD_ACTION = json.dumps({"owner_role": "safety_officer", "time_window": "within_1h",
                          "specific_action": "Move heavy outdoor tasks to before 10:00 or after 16:00.",
                          "escalation_if_no_ack_in_minutes": 60})


class FakeLLM:
    """Stands in for groq_client.chat. ``script`` maps kind -> list of replies (str or
    Exception), consumed in order; kind is inferred from the model / json_mode."""

    def __init__(self, **script):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[dict] = []

    def __call__(self, messages, *, model=None, max_tokens=400, temperature=0.3, json_mode=False,
                 cache=None, daily_budget=None, **kw):
        kind = "action" if json_mode else ("arabic" if model == G.model_ar() else "narration")
        self.calls.append({"kind": kind, "messages": messages, "max_tokens": max_tokens})
        if daily_budget is not None and cache is not None and cache.tokens_today() >= daily_budget:
            raise G.BudgetExceeded("budget")
        nxt = self.script[kind].pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        if cache is not None:
            cache.add_usage(150)
        return G.LLMReply(text=nxt, model=model or "m", prompt_tokens=100, completion_tokens=50)

    def kinds(self):
        return [c["kind"] for c in self.calls]


@pytest.fixture
def cache():
    c = G.NarrationCache(":memory:")
    yield c
    c.close()


def run(alert, fake, cache, monkeypatch, cfg=CFG, **kw):
    monkeypatch.setattr(L, "chat", fake)
    state = L.RunState(L.narration_config(cfg), cache)
    return L.enrich_alert(alert, SCEN[alert["scenario_id"]], state=state, **kw), state


# ---------------------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------------------
def test_prompt_includes_shap_drivers_severity_and_location():
    msgs = P.build_alert_narration_prompt(heat_alert(), HEAT)
    assert msgs[0]["role"] == "system" and "UAE public-health operations assistant" in msgs[0]["content"]
    assert [m["role"] for m in msgs[1:]] == ["user", "assistant", "user", "assistant", "user"]  # 2 few-shot
    user = msgs[-1]["content"]
    assert "Severity: advisory" in user
    assert "Al Quoz Industrial Area" in user and "Dubai" in user
    # SHAP shares (drivers), not the fusion-weight prior
    assert "apparent temperature: 54% (raises risk)" in user
    assert "worker absence rate: 31% (raises risk)" in user
    assert "heat-related clinic encounters in the catchment: 15% (lowers risk)" in user
    assert "45%" not in user and "35%" not in user
    # raw readings, owner role and local time
    assert "46.2 °C (usual 39.1 °C)" in user and "8.4% (usual 4.0%)" in user
    assert "Action owner: safety officer" in user
    assert "13:00 (afternoon)" in user


def test_prompt_is_pii_free():
    a = heat_alert(acks=[{"acknowledged_by": "Jane Doe"}], patient_id="P123")
    blob = json.dumps(P.build_alert_narration_prompt(a, HEAT)) + json.dumps(
        P.build_action_prompt(a, HEAT, "x"))
    assert "Jane Doe" not in blob and "P123" not in blob


def test_fewshot_fixtures_cover_every_scenario():
    for sid, sc in SCEN.items():
        ex = P.fewshot_examples(sid)
        assert len(ex) == 2, sid
        for e in ex:   # examples must pass the same validator as live output
            L.validate_narration(e["response"], P.format_facts(e["facts"]))
        assert set(sc["owner_roles"]["valid"]) <= set(P.OWNER_ROLES)


# ---------------------------------------------------------------------------------------
# happy path, fallbacks
# ---------------------------------------------------------------------------------------
def test_llm_path_fills_all_fields(cache, monkeypatch):
    fake = FakeLLM(narration=[GOOD_EN], arabic=[GOOD_AR], action=[GOOD_ACTION])
    a, _ = run(heat_alert(), fake, cache, monkeypatch)
    assert fake.kinds() == ["narration", "arabic", "action"]
    assert a["explanation_en"] == GOOD_EN and a["explanation_source"] == "llm"
    assert a["explanation_ar"] == GOOD_AR and a["explanation_ar_source"] == "llm"
    assert a["action"]["source"] == "llm" and a["action"]["owner_role"] == "safety_officer"
    assert a["action"]["escalation_if_no_ack_in_minutes"] == 60
    # per-alert completion budget: each call capped by what is left of 500
    assert [c["max_tokens"] for c in fake.calls] == [400, 300, 250]


def test_fallback_when_groq_raises(cache, monkeypatch):
    fake = FakeLLM(narration=[G.LLMOffline("connection refused")])
    a, state = run(heat_alert(), fake, cache, monkeypatch)
    assert fake.kinds() == ["narration"]        # offline: no further calls for this run
    assert a["explanation_source"] == "template_fallback"
    assert a["explanation_en"].startswith("Heat-stress cluster advisory issued for Al Quoz")
    assert a["explanation_ar_source"] == "template_fallback" and "54%" in a["explanation_ar"]
    act = a["action"]
    assert act["source"] == "template_fallback"
    assert act == {**act, "owner_role": "safety_officer", "time_window": "within_1h",
                   "specific_action": HEAT["recommended_actions"]["advisory"][0],
                   "escalation_if_no_ack_in_minutes": 60}
    assert state.offline


def test_no_api_key_means_template(cache, monkeypatch):
    a = heat_alert()
    L.enrich_alert(a, HEAT, state=L.RunState(L.narration_config(CFG), cache))   # real chat()
    assert a["explanation_source"] == "template_fallback"
    assert "GROQ_API_KEY" in a["narration"]["offline"]


def test_hallucinated_number_rejected_then_template(cache, monkeypatch):
    bad = GOOD_EN.replace("54%", "72%")
    fake = FakeLLM(narration=[bad, bad], action=[GOOD_ACTION])
    a, _ = run(heat_alert(), fake, cache, monkeypatch)
    assert fake.kinds()[:2] == ["narration", "narration"]
    assert a["explanation_source"] == "template_fallback"


def test_arabic_numbers_must_survive(cache, monkeypatch):
    lost = GOOD_AR.replace("46.2", "")
    indic = GOOD_AR.replace("54", "٥٤")              # Arabic-Indic digits are normalised, not rejected
    fake = FakeLLM(narration=[GOOD_EN], arabic=[lost, indic], action=[GOOD_ACTION])
    a, _ = run(heat_alert(), fake, cache, monkeypatch)
    assert fake.kinds() == ["narration", "arabic", "arabic", "action"]
    assert "strict" not in fake.calls[1]["messages"][0]["content"]
    assert "Western digits" in fake.calls[2]["messages"][0]["content"]
    assert a["explanation_ar_source"] == "llm" and "54%" in a["explanation_ar"]


def test_malformed_action_retried_tightened_then_fallback(cache, monkeypatch):
    fake = FakeLLM(narration=[GOOD_EN], arabic=[GOOD_AR],
                   action=["sure! here you go", json.dumps({"owner_role": "ceo", "time_window": "soon",
                                                            "specific_action": "Do it.",
                                                            "escalation_if_no_ack_in_minutes": -5})])
    a, _ = run(heat_alert(), fake, cache, monkeypatch, cfg={**CFG, "narration": {"per_alert_token_budget": 2000}})
    assert fake.kinds()[-2:] == ["action", "action"]
    assert "previous reply was rejected" in fake.calls[-1]["messages"][0]["content"]
    assert a["action"]["source"] == "template_fallback"
    assert a["explanation_source"] == "llm"            # one bad step does not sink the others


@pytest.mark.parametrize("payload, why", [
    ({"owner_role": "ceo"}, "owner enum"),
    ({"owner_role": "hr_lead"}, "valid enum but not a heat-scenario owner"),
    ({"time_window": "end of shift"}, "time window enum"),
    ({"time_window": "immediate"}, "too urgent for an advisory"),
    ({"escalation_if_no_ack_in_minutes": 0}, "not positive"),
    ({"escalation_if_no_ack_in_minutes": "60"}, "string, not int"),
    ({"specific_action": "Move tasks. Then call the inspector."}, "two sentences"),
    ({"extra": 1}, "extra key"),
])
def test_action_schema_rejects(payload, why):
    good = json.loads(GOOD_ACTION)
    with pytest.raises((ValidationError, ValueError)):
        L.parse_action(json.dumps({**good, **payload}), HEAT, "advisory")


def test_action_schema_accepts_fenced_json():
    act = L.parse_action(f"```json\n{GOOD_ACTION}\n```", HEAT, "advisory")
    assert act.time_window == "within_1h"


# ---------------------------------------------------------------------------------------
# cache + budgets
# ---------------------------------------------------------------------------------------
def test_cached_alert_makes_no_calls(cache, monkeypatch):
    run(heat_alert(), FakeLLM(narration=[GOOD_EN], arabic=[GOOD_AR], action=[GOOD_ACTION]),
        cache, monkeypatch)
    again = FakeLLM()
    a, _ = run(heat_alert(), again, cache, monkeypatch)
    assert again.calls == []
    assert a["explanation_en"] == GOOD_EN and a["action"]["source"] == "llm"
    # severity change => new narration key; same hex+scenario but new severity => new action
    esc = FakeLLM(narration=[G.LLMOffline("x")])
    b, _ = run(heat_alert(severity="warning"), esc, cache, monkeypatch)
    assert esc.kinds() == ["narration"] and b["explanation_source"] == "template_fallback"


def test_daily_budget_guard(cache, monkeypatch):
    cache.add_usage(100_000)
    fake = FakeLLM(narration=[GOOD_EN])
    a, state = run(heat_alert(), fake, cache, monkeypatch)
    assert a["explanation_source"] == "template_fallback"
    assert "budget" in state.offline


def test_per_alert_budget_stops_calls(cache, monkeypatch):
    fake = FakeLLM(narration=[GOOD_EN], arabic=[GOOD_AR], action=[GOOD_ACTION])
    a, _ = run(heat_alert(), fake, cache, monkeypatch,
               cfg={**CFG, "narration": {"per_alert_token_budget": 120}})
    assert fake.kinds() == ["narration"]               # 120 - 50 = 70 < MIN_CALL_TOKENS
    assert a["explanation_source"] == "llm"
    assert a["explanation_ar_source"] == "template_fallback"
    assert a["action"]["source"] == "template_fallback"


def test_action_cache_expires_after_6h(monkeypatch):
    now = [datetime(2026, 8, 20, 9, tzinfo=timezone.utc)]
    c = G.NarrationCache(":memory:", now=lambda: now[0])
    c.put_action("k", {"x": 1})
    assert c.get_action("k", 6) == {"x": 1}
    now[0] = now[0].replace(hour=15, minute=1)
    assert c.get_action("k", 6) is None


# ---------------------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------------------
class FakeResp:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body or {}, headers or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses, self.posts = list(responses), []

    def post(self, url, json=None, timeout=None, headers=None):
        self.posts.append(json)
        return self.responses.pop(0)


OK = FakeResp(200, {"model": "m", "choices": [{"message": {"content": "hello"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})


def test_chat_retries_429_with_backoff(cache, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    s = FakeSession([FakeResp(429), FakeResp(429, headers={"retry-after": "3"}), OK])
    waits = []
    r = G.chat([{"role": "user", "content": "x"}], model="openai/gpt-oss-120b", cache=cache,
               session=s, sleep=waits.append)
    assert r.text == "hello" and len(s.posts) == 3
    assert 1.0 <= waits[0] <= 1.25 and waits[1] == 3.0
    assert s.posts[0]["reasoning_effort"] == "low"
    assert cache.tokens_today() == 15


def test_chat_gives_up_after_three_retries(cache, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    s = FakeSession([FakeResp(429)] * 4)
    with pytest.raises(G.LLMOffline):
        G.chat([{"role": "user", "content": "x"}], cache=cache, session=s, sleep=lambda _: None)
    assert len(s.posts) == 4


def test_narrate_caches_by_alert_and_prompt(cache, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    s = FakeSession([OK])
    msgs = [{"role": "user", "content": "x"}]
    assert G.narrate(msgs, alert_id="a", cache=cache, session=s) == "hello"
    assert G.narrate(msgs, alert_id="a", cache=cache, session=FakeSession([])) == "hello"
    out = G.narrate_with_fallback([{"role": "user", "content": "other"}], "TEMPLATE", alert_id="a",
                                  cache=cache, session=FakeSession([FakeResp(500)] * 4),
                                  sleep=lambda _: None)
    assert out["text"] == "TEMPLATE" and out["source"] == "template_fallback"
