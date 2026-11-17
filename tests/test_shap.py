"""SHAP explainability (packages/pipelines/explain) on a synthetic world whose worker
absence is generated from the absence_model equations in configs/workforce_mix.yaml.

Two sites, 45 days. Site Dubai has recurring heat episodes ending in a 48 °C
apparent-temperature event at as_of; site Abu Dhabi has recurring dust episodes ending
in a PM2.5 ~170 µg/m³ event at as_of. Fully offline.
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

pytest.importorskip("prophet")
pytest.importorskip("lightgbm")
pytest.importorskip("shap")

from packages.pipelines.explain import shap_runner as S  # noqa: E402
from packages.pipelines.explain.narrate import narrate_alert_template  # noqa: E402
from packages.pipelines.fusion import alerts as A  # noqa: E402
from packages.pipelines.fusion.config import load_scenarios  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CFG = load_scenarios(ROOT / "configs" / "scenarios.yaml")
ABSENCE = yaml.safe_load((ROOT / "configs" / "workforce_mix.yaml").read_text())["absence_model"]
AS_OF = pd.Timestamp("2026-08-30T10:00:00Z")
START = AS_OF - pd.Timedelta(days=45)
EVENT_HOURS = 6
WORKERS = 150


def _logit(p):
    return np.log(p / (1 - p))


def _sigmoid(x):
    return 1 / (1 + np.exp(-x))


def absence_probs(at, pm25, pm10):
    """The workforce_mix.yaml absence_model, read the same way as synth/workforce.py:
    intercepts are base probabilities, slopes log-odds, gated to 0 with no excess."""
    h, r = ABSENCE["heat"], ABSENCE["respiratory"]
    xa = np.clip(at - h["apparent_temp_threshold"], 0, None)
    xp = np.clip(pm25 - h["pm25_threshold"], 0, None) / 10
    heat = _sigmoid(_logit(h["intercept_prob"]) + h["apparent_temp_slope"] * xa + h["pm25_slope_per_10"] * xp)
    heat = np.where((xa > 0) | (xp > 0), heat, 0.0)
    rp = np.clip(pm25 - r["pm25_threshold"], 0, None) / 10
    rq = np.clip(pm10 - r["pm10_threshold"], 0, None) / 20
    resp = _sigmoid(_logit(r["intercept_prob"]) + r["pm25_slope_per_10"] * rp + r["pm10_slope_per_20"] * rq)
    resp = np.where((rp > 0) | (rq > 0), resp, 0.0)
    return heat, resp


def _episodes(ts, rng, n, last_peak, *, hours=EVENT_HOURS):
    """n earlier episodes (random days, 09-15 local) plus one ending at as_of.
    Returns an intensity in [0, 1] per hour."""
    k = np.zeros(len(ts))
    days = rng.choice(np.arange(3, 40), size=n, replace=False)
    for d in days:
        t0 = START + pd.Timedelta(days=int(d), hours=5)
        m = (ts >= t0) & (ts < t0 + pd.Timedelta(hours=hours))
        k[m] = rng.uniform(0.4, 0.9)
    k[ts > AS_OF - pd.Timedelta(hours=hours)] = last_peak
    return k


def _world(seed: int = 7):
    rng = np.random.default_rng(seed)
    ts = pd.date_range(START, AS_OF, freq="h")
    hour = ts.hour.to_numpy()
    sites = pd.DataFrame([
        {"site_id": "site-dxb", "name": "Hot Site Dubai", "operator": "x", "sector": "construction",
         "emirate": "Dubai", "lat": 25.10, "lon": 55.20, "h3_res8": "", "worker_count": 400,
         "shift_pattern": "double_12h", "heat_exposure": "outdoor"},
        {"site_id": "site-auh", "name": "Dusty Site Abu Dhabi", "operator": "x", "sector": "ports",
         "emirate": "Abu Dhabi", "lat": 24.50, "lon": 54.40, "h3_res8": "", "worker_count": 300,
         "shift_pattern": "triple_8h", "heat_exposure": "mixed"},
    ])
    env, feats, heat_k, dust_k = [], [], {}, {}
    for sid in sites["site_id"]:
        at = 33 + 5 * np.sin((hour - 4) / 24 * 2 * np.pi) + rng.normal(0, 0.8, len(ts))
        pm25 = 25 + rng.normal(0, 3, len(ts))
        pm10 = 70 + rng.normal(0, 8, len(ts))
        if sid == "site-dxb":                       # heat: up to +15 °C -> 48 °C at as_of
            k = _episodes(ts, rng, 6, 1.0)
            at = at + 15 * k
            at[ts > AS_OF - pd.Timedelta(hours=EVENT_HOURS)] = 48 + rng.normal(0, 0.3, EVENT_HOURS)
            heat_k[sid] = k
        else:                                       # dust: PM2.5 ~170, PM10 ~420 at as_of
            k = _episodes(ts, rng, 6, 1.0)
            pm25 = pm25 + 145 * k
            pm10 = pm10 + 350 * k
            dust_k[sid] = k
        p_heat, p_resp = absence_probs(at, pm25, pm10)
        base = sum(ABSENCE["baseline"].values())
        heat_abs = rng.binomial(WORKERS, p_heat) / WORKERS
        resp_abs = rng.binomial(WORKERS, p_resp) / WORKERS
        other = rng.binomial(WORKERS, base, len(ts)) / WORKERS
        env.append(pd.DataFrame({"site_id": sid, "ts_utc": ts, "apparent_temp": at,
                                 "pm25": pm25, "pm10": pm10}))
        feats.append(pd.DataFrame({"site_id": sid, "ts_utc": ts, "scheduled_on_site": WORKERS,
                                   "absence_rate_last_1h": (heat_abs + resp_abs + other).clip(0, 1),
                                   "heat_absence_rate_last_1h": heat_abs,
                                   "respiratory_absence_rate_last_1h": resp_abs}))
    patients = pd.DataFrame({"patient_id": [f"p{i}" for i in range(400)],
                             "city": ["Dubai"] * 250 + ["Abu Dhabi"] * 150})
    rows, eid = [], 0
    for i, t in enumerate(ts):
        for city, lam, sid in (("Dubai", 2.0, "site-dxb"), ("Abu Dhabi", 1.2, "site-auh")):
            pool = patients[patients["city"] == city]["patient_id"].to_numpy()
            extra = rng.poisson(1.5 * heat_k[sid][i]) if sid in heat_k else rng.poisson(1.5 * dust_k[sid][i])
            for j in range(rng.poisson(lam) + extra):
                eid += 1
                code = None
                if j >= rng.poisson(lam):                       # the excess presentations
                    code = "34095006" if sid in heat_k else "195967001"   # dehydration / asthma
                rows.append({"patient_id": rng.choice(pool), "event_type": "encounter",
                             "code": "x", "encounter_id": f"e{eid}",
                             "event_time": t + pd.Timedelta(minutes=int(rng.integers(0, 60))),
                             "reason_code": code})
    return dict(sites=sites, env=pd.concat(env, ignore_index=True),
                features=pd.concat(feats, ignore_index=True), events=pd.DataFrame(rows),
                patients=patients)


@pytest.fixture(scope="module")
def world():
    return _world()


@pytest.fixture(scope="module")
def run(world, tmp_path_factory):
    out = tmp_path_factory.mktemp("alerts")
    cache = tmp_path_factory.mktemp("cache")
    alerts = A.generate_alerts(AS_OF, CFG, out_dir=out, cache_dir=cache,
                               explain_dir=cache / "explain", panel_kwargs=world)
    return alerts, out, cache


def _alert(alerts, scenario, site):
    hits = [a for a in alerts if a["scenario_id"] == scenario and a["sites_affected"] == [site]]
    assert hits, f"no {scenario} alert at {site}; got {[(a['scenario_id'], a['sites_affected']) for a in alerts]}"
    return hits[0]


# ---------------------------------------------------------------------------
# validation required by the brief
# ---------------------------------------------------------------------------

def test_shap_contributions_sum_to_one(run):
    alerts, _, _ = run
    assert alerts
    for a in alerts:
        assert a["drivers_method"] == "shap", a["id"]
        pct = [v["pct_of_total"] for v in a["shap"]["signals"].values()]
        assert sum(pct) == pytest.approx(1.0, abs=0.02)
        assert sum(a["drivers"].values()) == pytest.approx(100, abs=2)
        assert set(a["drivers"]) == set(a["drivers_prior"]) == set(a["driver_details"])
        # TreeSHAP is exact: base + sum(SHAP) reproduces the surrogate's output
        sh = a["shap"]
        assert sh["base_value"] + sum(sh["features"].values()) == pytest.approx(sh["prediction"], abs=1e-4)
        assert sh["ms"] < 300


def test_48c_heat_alert_is_driven_by_apparent_temperature(run, world):
    alerts, _, _ = run
    env = world["env"]
    peak_at = env[(env["site_id"] == "site-dxb") & (env["ts_utc"] == AS_OF)]["apparent_temp"].iloc[0]
    assert peak_at == pytest.approx(48, abs=1.5)
    a = _alert(alerts, "heat-stress-cluster", "site-dxb")
    sig = a["shap"]["signals"]
    assert sig["apparent_temp"]["shap"] > 0 and sig["apparent_temp"]["direction"] == "raises_risk"
    assert max(sig, key=lambda k: sig[k]["abs_shap"]) == "apparent_temp"


def test_shap_recovers_absence_model_weights_on_two_scenarios(run, world):
    """Round trip: absence was generated by the workforce_mix.yaml absence model.
    On the 48 °C hour heat dominates; on the dust hour PM2.5 dominates, and PM2.5 outranks
    PM10 as it does in both the fusion weights and the respiratory log-odds terms."""
    alerts, _, _ = run
    heat = _alert(alerts, "heat-stress-cluster", "site-dxb")
    dust = _alert(alerts, "respiratory-dust-event", "site-auh")

    # 1) heat-stress: the heat log-odds term is what moved absence at site-dxb ...
    env = world["env"].set_index(["site_id", "ts_utc"])
    at, p25, p10 = (env.loc[("site-dxb", AS_OF), c] for c in ("apparent_temp", "pm25", "pm10"))
    p_heat, p_resp = absence_probs(np.array([at]), np.array([p25]), np.array([p10]))
    assert p_heat[0] > 5 * max(p_resp[0], 1e-6)
    # ... and SHAP puts the heat drivers (temperature, then absence) on top.
    hs = heat["shap"]["signals"]
    top2 = sorted(hs, key=lambda k: -hs[k]["abs_shap"])[:2]
    assert set(top2) == {"apparent_temp", "absence_rate"}
    assert hs["apparent_temp"]["pct_of_total"] >= 0.4

    # 2) respiratory-dust: the respiratory term dominates at site-auh ...
    at, p25, p10 = (env.loc[("site-auh", AS_OF), c] for c in ("apparent_temp", "pm25", "pm10"))
    r = ABSENCE["respiratory"]
    pm25_term = r["pm25_slope_per_10"] * max(p25 - r["pm25_threshold"], 0) / 10
    pm10_term = r["pm10_slope_per_20"] * max(p10 - r["pm10_threshold"], 0) / 20
    assert pm25_term > pm10_term > 0
    # ... and SHAP makes PM2.5 the largest raiser of risk, ahead of PM10.
    ds = dust["shap"]["signals"]
    assert max(ds, key=lambda k: ds[k]["abs_shap"]) == "pm25"
    assert ds["pm25"]["direction"] == "raises_risk"
    assert ds["pm25"]["abs_shap"] > ds["pm10"]["abs_shap"]

    # cross-check: no dust alert at the hot site. A heat-stress alert at the dusty site can
    # happen (its afternoons touch 39 °C, above the 38 °C floor), but then SHAP must blame
    # the temperature, not the dust-driven absence.
    assert not any(a["scenario_id"] == "respiratory-dust-event" and a["sites_affected"] == ["site-dxb"]
                   for a in alerts)
    for a in alerts:
        if a["scenario_id"] == "heat-stress-cluster" and a["sites_affected"] == ["site-auh"]:
            s = a["shap"]["signals"]
            assert a["driver_details"]["apparent_temp"]["value"] > 38
            assert max(s, key=lambda k: s[k]["abs_shap"]) == "apparent_temp"


# ---------------------------------------------------------------------------
# behaviour
# ---------------------------------------------------------------------------

def test_both_tasks_fitted_and_prior_kept(run):
    alerts, out, _ = run
    a = _alert(alerts, "heat-stress-cluster", "site-dxb")
    w = {s["name"]: s["weight"] for s in next(s for s in CFG["scenarios"]
                                              if s["id"] == "heat-stress-cluster")["signals"]}
    sc = {k: v["score"] for k, v in a["driver_details"].items()}
    expect = {k: 100 * w[k] * sc[k] / sum(w[j] * sc[j] for j in w) for k in w}
    assert a["drivers_prior"] == {k: pytest.approx(v, abs=0.1) for k, v in expect.items()}
    assert a["shap"]["task"] == "regression" and a["shap"]["model"]["r2_holdout"] > 0.8
    assert a["drivers_severity"] and a["shap_severity"]["task"] == "classification"
    assert a["explanation_template"].startswith("Heat-stress cluster ")
    assert "apparent temperature" in a["explanation_template"]
    meta = (out / "meta.json").read_text()
    assert "surrogates" in meta and "shap_ms_max" in meta


def test_surrogate_cache_reuse_and_retrain_window(world, run, caplog):
    _, _, cache = run
    ed = cache / "explain"
    assert (ed / "surrogate_heat-stress-cluster_regression.pkl").is_file()
    assert (ed / "surrogate_heat-stress-cluster_classification.pkl").is_file()
    with caplog.at_level(logging.INFO, logger="sentinel.explain.shap"):
        A.generate_alerts(AS_OF + pd.Timedelta(hours=1), CFG, out_dir=None, cache_dir=cache,
                          explain_dir=ed, panel_kwargs=world)
    assert "reused surrogate" in caplog.text                      # 1 h later: within 12 h

    payload = S._load(S.surrogate_path(ed, "heat-stress-cluster", "regression"))
    h = payload["config_hash"]
    fitted = pd.Timestamp(payload["model_fitted_at"])
    assert S._usable(payload, fitted + pd.Timedelta(hours=12), h, 12)
    assert not S._usable(payload, fitted + pd.Timedelta(hours=13), h, 12)       # window moved
    assert not S._usable(payload, fitted - pd.Timedelta(hours=1), h, 12)        # never from the future
    assert not S._usable(payload, fitted, "other-config", 12)


def test_surrogate_failure_falls_back_to_fusion_weights(world, tmp_path):
    cfg = {**CFG, "explain": {**CFG.get("explain", {}), "min_train_rows": 10 ** 9}}
    alerts = A.generate_alerts(AS_OF, cfg, out_dir=None, cache_dir=tmp_path,
                               explain_dir=tmp_path / "explain", panel_kwargs=world)
    assert alerts
    for a in alerts:
        assert a["drivers_method"] == "fusion_weights" and a["shap"] is None
        assert a["drivers"] == a["drivers_prior"] and a["drivers"]
        assert "fusion weights" in a["explanation_template"]


def test_disagreement_is_logged_not_overridden(monkeypatch, caplog):
    sc = next(s for s in CFG["scenarios"] if s["id"] == "respiratory-dust-event")
    fake = {"pm25": {"abs_shap": 0.02, "shap": -0.02, "pct_of_total": 0.2, "direction": "lowers_risk"},
            "pm10": {"abs_shap": 0.08, "shap": 0.08, "pct_of_total": 0.8, "direction": "raises_risk"}}
    feats = pd.Series({"pm25__z": -0.02, "pm10__z": 0.08})
    monkeypatch.setattr(A, "per_alert_contributions", lambda *a, **k: (fake, feats, 0.3))
    names = ["pm25__z", "pm10__z"]
    ts = AS_OF
    X = pd.DataFrame({"pm25__z": [1.0], "pm10__z": [2.0]},
                     index=pd.MultiIndex.from_tuples([("hex", ts)], names=["unit_h3", "ts_utc"]))
    ctx = {"X": X, "regression": (object(), X.reset_index(drop=True), names),
           "classification": None, "explainers": {}}
    alert = {"id": "a1"}
    with caplog.at_level(logging.WARNING, logger="sentinel.fusion.alerts"):
        A._explain(alert, sc, ctx, {"pm25": 70.0, "pm10": 30.0}, "hex", ts, CFG)
    assert "pm25 LOWERS risk" in caplog.text
    assert alert["drivers"] == {"pm25": 20.0, "pm10": 80.0}                 # SHAP stands
    assert alert["shap_disagreements"][0]["signal"] == "pm25"


def test_lag_features_collapse_into_named_signal():
    sc = next(s for s in CFG["scenarios"] if s["id"] == "respiratory-dust-event")
    names = S.feature_names_for(sc)
    assert {"pm25__value", "pm25__z", "pm25__score", "pm25__z_lag1", "pm25__z_lag3"} <= set(names)
    row = pd.Series({n: 0.01 for n in names})
    net = S.collapse_to_signals(row)
    assert set(net) == {s["name"] for s in sc["signals"]}
    assert net["pm25"] == pytest.approx(0.05)
    wf = S.waterfall(row, 0.3, level="signal")
    assert len(wf["steps"]) == 4 and wf["prediction"] == pytest.approx(0.3 + 0.01 * len(names))
    assert wf["steps"][0]["baseline"] == 0.3
    assert wf["steps"][-1]["cumulative"] == pytest.approx(wf["prediction"])


def test_build_features_lags_are_time_based():
    sc = next(s for s in CFG["scenarios"] if s["id"] == "heat-stress-cluster")
    ts = pd.to_datetime(["2026-08-01T00:00Z", "2026-08-01T01:00Z", "2026-08-01T03:00Z"])  # 02:00 missing
    frame = pd.DataFrame({"unit_h3": "h", "ts_utc": ts, "apparent_temp__z": [1.0, 2.0, 4.0]})
    f = S.build_features(frame, sc).set_index("ts_utc")
    assert f.loc[ts[1], "apparent_temp__z_lag1"] == 1.0
    assert np.isnan(f.loc[ts[2], "apparent_temp__z_lag1"])               # 02:00 is a gap
    assert f.loc[ts[2], "apparent_temp__z_lag3"] == 1.0


def test_narration_template_shape():
    a = {"scenario_label": "Heat-stress cluster", "severity": "watch",
         "site_names": ["Al Quoz Industrial Area"],
         "drivers": {"apparent_temp": 54, "pm25": 31, "absence_rate": 15},
         "shap": {"signals": {k: {"direction": "raises_risk"} for k in ("apparent_temp", "pm25", "absence_rate")}},
         "driver_onsets": {k: "2026-08-30T05:00:00Z" for k in ("apparent_temp", "pm25", "absence_rate")},
         "drivers_method": "shap"}
    assert narrate_alert_template(a) == (
        "Heat-stress cluster watch issued for Al Quoz Industrial Area. The alert is driven "
        "primarily by apparent temperature (54% of signal), with secondary contribution from "
        "PM2.5 (31%) and absence rate (15%). All three have been rising since 09:00 local time.")
    a["shap"]["signals"]["absence_rate"]["direction"] = "lowers_risk"
    assert "Absence rate is below its usual level" in narrate_alert_template(a)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(run, tmp_path, monkeypatch):
    _, out, _ = run
    monkeypatch.setenv("ALERTS_DATA_DIR", str(out))
    monkeypatch.setenv("ALERTS_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("WEBSITE_SITE_NAME", raising=False)
    spec = importlib.util.spec_from_file_location("alerts_router_shap",
                                                  ROOT / "apps" / "api" / "routers" / "alerts.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(mod.router, prefix="/api/alerts")
    return TestClient(app)


def test_api_detail_and_waterfall(client):
    body = client.get("/api/alerts", params={"include_expired": True}).json()
    a = next(x for x in body["alerts"] if x["scenario_id"] == "heat-stress-cluster")
    assert a["drivers_method"] == "shap" and a["drivers_prior"] and a["explanation_template"]
    d = client.get(f"/api/alerts/{a['id']}").json()["alert"]
    for k in ("drivers", "drivers_prior", "explanation_template", "shap", "driver_onsets"):
        assert d[k], k
    w = client.get(f"/api/alerts/{a['id']}/shap_waterfall").json()
    assert w["level"] == "signal" and {s["feature"] for s in w["steps"]} == set(a["drivers"])
    assert w["steps"][0]["baseline"] == pytest.approx(w["base_value"])
    assert w["steps"][-1]["cumulative"] == pytest.approx(w["prediction"])
    for s0, s1 in zip(w["steps"], w["steps"][1:]):
        assert s1["baseline"] == pytest.approx(s0["cumulative"])
    raw = client.get(f"/api/alerts/{a['id']}/shap_waterfall", params={"level": "feature"}).json()
    assert len(raw["steps"]) == 5 * len(a["drivers"]) and raw["prediction"] == pytest.approx(w["prediction"])
    sev = client.get(f"/api/alerts/{a['id']}/shap_waterfall", params={"task": "classification"})
    assert sev.status_code == 200 and sev.json()["output"].startswith("log-odds")
    assert client.get(f"/api/alerts/{a['id']}/shap_waterfall", params={"level": "x"}).status_code == 422
    assert client.get("/api/alerts/nope/shap_waterfall").status_code == 404
