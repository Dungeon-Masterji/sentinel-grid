"""Tests for the tri-signal fusion detector (packages/pipelines/fusion) and /api/alerts.

Fully offline: a small synthetic world (2 sites, 45 days) with an injected heat event.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("prophet")
pytest.importorskip("pyod")

from packages.pipelines.fusion import alerts as A  # noqa: E402
from packages.pipelines.fusion import baselines as B  # noqa: E402
from packages.pipelines.fusion import detect as D  # noqa: E402
from packages.pipelines.fusion.combine import contributions, fuse  # noqa: E402
from packages.pipelines.fusion.config import load_scenarios, unique_signals  # noqa: E402
from packages.pipelines.fusion.spatial import aggregate_to_h3, hex_boundary_geojson  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CFG = load_scenarios(ROOT / "configs" / "scenarios.yaml")
AS_OF = pd.Timestamp("2026-08-30T10:00:00Z")
START = AS_OF - pd.Timedelta(days=45)
SPIKE_FROM = AS_OF - pd.Timedelta(hours=5)


# ---------------------------------------------------------------------------
# synthetic world
# ---------------------------------------------------------------------------

def _sites():
    return pd.DataFrame([
        {"site_id": "site-dxb", "name": "Test Site Dubai", "operator": "x", "sector": "construction",
         "emirate": "Dubai", "lat": 25.10, "lon": 55.20, "h3_res8": "", "worker_count": 400,
         "shift_pattern": "double_12h", "heat_exposure": "outdoor"},
        {"site_id": "site-auh", "name": "Test Site Abu Dhabi", "operator": "x", "sector": "ports",
         "emirate": "Abu Dhabi", "lat": 24.50, "lon": 54.40, "h3_res8": "", "worker_count": 300,
         "shift_pattern": "triple_8h", "heat_exposure": "mixed"},
    ])


def _world(seed: int = 0):
    rng = np.random.default_rng(seed)
    ts = pd.date_range(START, AS_OF, freq="h")
    hour = ts.hour.to_numpy()
    env, feats = [], []
    for sid, spike in (("site-dxb", True), ("site-auh", False)):
        at = 33 + 6 * np.sin((hour - 4) / 24 * 2 * np.pi) + rng.normal(0, 0.8, len(ts))
        ab = 0.02 + rng.normal(0, 0.004, len(ts)).clip(-0.015, None)
        if spike:
            hot = ts >= SPIKE_FROM
            at = np.where(hot, at + 14, at)
            ab = np.where(hot, ab + 0.10, ab)
        env.append(pd.DataFrame({"site_id": sid, "ts_utc": ts, "apparent_temp": at,
                                 "pm25": 25 + rng.normal(0, 3, len(ts)),
                                 "pm10": 70 + rng.normal(0, 8, len(ts))}))
        feats.append(pd.DataFrame({"site_id": sid, "ts_utc": ts, "scheduled_on_site": 150,
                                   "absence_rate_last_1h": ab.clip(0, 1),
                                   "heat_absence_rate_last_1h": (ab - 0.02).clip(0, 1),
                                   "respiratory_absence_rate_last_1h": 0.004}))
    patients = pd.DataFrame({"patient_id": [f"p{i}" for i in range(400)],
                             "city": ["Dubai"] * 250 + ["Abu Dhabi"] * 150})
    rows = []
    eid = 0
    for t in ts:
        for city, lam in (("Dubai", 2.0), ("Abu Dhabi", 1.0)):
            pool = patients[patients["city"] == city]["patient_id"].to_numpy()
            n = rng.poisson(lam)
            hot = city == "Dubai" and t >= SPIKE_FROM
            for _ in range(n + (2 if hot else 0)):
                eid += 1
                rows.append({"patient_id": rng.choice(pool), "event_type": "encounter",
                             "code": "x", "encounter_id": f"e{eid}",
                             "event_time": t + pd.Timedelta(minutes=int(rng.integers(0, 60))),
                             "reason_code": "34095006" if hot else None})
    events = pd.DataFrame(rows)
    return dict(sites=_sites(), env=pd.concat(env, ignore_index=True),
                features=pd.concat(feats, ignore_index=True), events=events, patients=patients)


@pytest.fixture(scope="module")
def world():
    return _world()


@pytest.fixture(scope="module")
def run(world, tmp_path_factory):
    out = tmp_path_factory.mktemp("alerts")
    cache = tmp_path_factory.mktemp("cache")
    alerts = A.generate_alerts(AS_OF, CFG, out_dir=out, cache_dir=cache, explain_dir=cache / "explain",
                               panel_kwargs=world)
    return alerts, out, cache


# ---------------------------------------------------------------------------
# spatial + privacy
# ---------------------------------------------------------------------------

def test_aggregate_to_h3_suppresses_small_cells():
    t = pd.Timestamp("2026-08-01T10:00Z")
    df = pd.DataFrame({"lat": [25.1] * 4 + [24.5] * 6, "lon": [55.2] * 4 + [54.4] * 6,
                       "t": [t] * 10, "v": [1.0] * 10})
    a = aggregate_to_h3(df, "lat", "lon", "t", "v", agg="sum", min_records=5)
    small = a[a["suppressed"]]
    big = a[~a["suppressed"]]
    assert len(small) == 1 and small["value"].isna().all() and small["n_records"].isna().all()
    assert big["value"].iloc[0] == 6 and big["n_records"].iloc[0] == 6
    # non-personal (env) data may opt out with min_records=1
    assert not aggregate_to_h3(df, "lat", "lon", "t", "v", min_records=1)["suppressed"].any()


def test_aggregate_window_counts_trailing_24h():
    t0 = pd.Timestamp("2026-08-01T00:00Z")
    df = pd.DataFrame({"lat": 25.1, "lon": 55.2, "t": [t0 + pd.Timedelta(hours=h) for h in range(6)]})
    a = aggregate_to_h3(df, "lat", "lon", "t", None, agg="count", window="24h",
                        start=t0, end=t0 + pd.Timedelta(hours=30), min_records=5)
    s = a.set_index("ts_utc")
    assert s.loc[t0 + pd.Timedelta(hours=3), "suppressed"]                    # 4 records
    assert s.loc[t0 + pd.Timedelta(hours=5), "value"] == 6
    assert s.loc[t0 + pd.Timedelta(hours=29), "suppressed"]                   # window rolled off


def test_hex_boundary_is_closed_polygon():
    g = hex_boundary_geojson("8743a104affffff")
    ring = g["coordinates"][0]
    assert g["type"] == "Polygon" and ring[0] == ring[-1] and len(ring) == 7
    assert 54 < ring[0][0] < 56 and 24 < ring[0][1] < 26                       # lon, lat order


# ---------------------------------------------------------------------------
# baselines
# ---------------------------------------------------------------------------

def _series(days: int, spike_at: int | None = None, gap_day: int | None = None):
    ts = pd.date_range("2026-07-01", periods=days * 24, freq="h", tz="UTC")
    v = 30 + 5 * np.sin(ts.hour.to_numpy() / 24 * 2 * np.pi)
    v = v + np.random.default_rng(1).normal(0, 0.3, len(ts))
    if spike_at is not None:
        v[spike_at] += 15
    df = pd.DataFrame({"h3": "a", "ts_utc": ts, "value": v})
    if gap_day is not None:
        df = df[~df["ts_utc"].dt.day.eq(gap_day)]
    return df


def test_rolling_z_has_no_lookahead():
    s = _series(10).set_index("ts_utc")["value"]
    z1 = B.rolling_z(s, 30)
    s2 = s.copy()
    s2.iloc[-1] = 999
    z2 = B.rolling_z(s2, 30)
    pd.testing.assert_series_equal(z1.iloc[:-1], z2.iloc[:-1])
    flat = pd.Series(0.0, index=s.index)
    assert B.rolling_z(flat, 30).dropna().empty                               # std 0 -> NaN
    assert (B.rolling_z(flat, 30, min_std=1.0).dropna() == 0).all()


def test_prophet_baseline_fits_flags_spike_and_keeps_gaps(tmp_path):
    df = _series(40, spike_at=35 * 24 + 5, gap_day=20)
    model, res = B.fit_prophet_baseline(df, "t_sig", cache_dir=tmp_path)
    assert model.kind == "prophet" and (tmp_path / "t_sig.pkl").is_file()
    gap = res[res["ts_utc"].dt.day.eq(20) & res["ts_utc"].dt.month.eq(7)]
    assert len(gap) == 24 and gap["value"].isna().all() and gap["z"].isna().all()   # not imputed
    spike = res.loc[res["value"].idxmax()]
    assert spike["z"] > 8
    assert abs(spike["baseline"] - 30) < 6                                    # winsorised fit
    # pickle round-trip keeps the model usable
    cached = B.load_cached("t_sig", tmp_path)
    r2 = cached.residuals(df)
    np.testing.assert_allclose(r2["baseline"].dropna().to_numpy(), res["baseline"].dropna().to_numpy())


def test_short_history_falls_back_to_rolling_z(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="sentinel.fusion.baselines"):
        model, res = B.fit_prophet_baseline(_series(10), "short", cache_dir=None)
    assert model.kind == "rolling_z"
    assert "rolling_z fallback" in caplog.text
    assert res["z"].notna().sum() > 100


# ---------------------------------------------------------------------------
# detectors
# ---------------------------------------------------------------------------

def test_detector_ensemble_scores_and_gating():
    z = np.random.default_rng(0).normal(0, 1, 3000)
    det = D.DetectorEnsemble().fit(z)
    s = det.score(np.array([0.0, 1.0, 6.0, np.nan]))
    assert 0 <= s[0] < 0.2 and s[2] > 0.9 and np.isnan(s[3])
    g = D.score_signal([-6.0, 6.0, 6.0], det, values=[50, 50, 30], floor=38)
    assert g[0] == 0 and g[1] > 0.9 and g[2] == 0                              # down-tail, below floor


def test_fit_all_fits_each_unique_signal_once(world, monkeypatch, tmp_path):
    from packages.pipelines.fusion.signals import build_signal_panel
    panel, _ = build_signal_panel(AS_OF, CFG, **world)
    calls = []
    real = D.fit_prophet_baseline

    def spy(df, name, **kw):
        calls.append(name)
        return real(df, name, **kw)

    monkeypatch.setattr(D, "fit_prophet_baseline", spy)
    fitted = D.fit_all(CFG, panel, AS_OF, cache_dir=tmp_path)
    assert sorted(calls) == sorted(set(calls)) == sorted(unique_signals(CFG))
    assert "absence_rate" in fitted                               # shared by 2 scenarios, fitted once
    # second call reuses the cache; a replay EARLIER than the cache refits (no future models)
    calls.clear()
    D.fit_all(CFG, panel, AS_OF, cache_dir=tmp_path)
    assert calls == []
    D.fit_all(CFG, panel, AS_OF - pd.Timedelta(days=2), cache_dir=tmp_path)
    assert sorted(calls) == sorted(unique_signals(CFG))


# ---------------------------------------------------------------------------
# fusion
# ---------------------------------------------------------------------------

def test_fuse_map_orders_and_quantifies_uncertainty():
    w = {"a": 0.45, "b": 0.35, "c": 0.20}
    rng = np.random.default_rng(0)
    hist = pd.DataFrame(rng.beta(0.5, 6, size=(500, 3)), columns=list(w))
    probe = pd.DataFrame([[0.01, 0.01, 0.01], [0.95, 0.9, 0.8], [0.95, np.nan, np.nan],
                          [np.nan, np.nan, np.nan]], columns=list(w))
    out = fuse(pd.concat([hist, probe], ignore_index=True), w)
    low, high, partial, empty = (out.iloc[-4], out.iloc[-3], out.iloc[-2], out.iloc[-1])
    assert low["mean"] < 0.5 < high["mean"]
    assert low["ci_low"] <= low["mean"] <= low["ci_high"]
    assert (partial["ci_high"] - partial["ci_low"]) > (high["ci_high"] - high["ci_low"])
    assert partial["coverage"] == pytest.approx(0.45)
    assert np.isnan(empty["mean"])
    c = contributions({"a": 0.9, "b": 0.1, "c": np.nan}, w)
    assert set(c) == {"a", "b"} and sum(c.values()) == pytest.approx(100, abs=0.2)


@pytest.mark.skipif(not os.environ.get("RUN_SLOW"), reason="PyMC sampling is slow; set RUN_SLOW=1")
def test_fuse_sampled_matches_map():
    w = {"a": 0.6, "b": 0.4}
    rng = np.random.default_rng(1)
    S = pd.DataFrame(rng.beta(0.5, 6, size=(200, 2)), columns=list(w))
    S.iloc[-1] = [0.95, 0.9]
    m = fuse(S, w, mode="map", rows=np.array([len(S) - 1]))
    s = fuse(S, w, mode="sampled", rows=np.array([len(S) - 1]),
             sample_kwargs={"draws": 300, "tune": 300, "chains": 1})
    assert abs(m["mean"].iloc[0] - s["mean"].iloc[0]) < 0.1


def test_ineligible_hex_never_alerts():
    frame = pd.DataFrame({"unit_h3": ["x"] * 2, "ts_utc": [AS_OF] * 2,
                          "apparent_temp__n_records": [np.nan, 1.0],
                          "absence_rate__n_records": [np.nan, np.nan],
                          "heat_related_encounter_count__n_records": [np.nan, np.nan]})
    assert A._eligible(frame, CFG).tolist() == [False, True]


# ---------------------------------------------------------------------------
# end to end
# ---------------------------------------------------------------------------

def test_injected_heat_event_raises_explainable_alert(run):
    alerts, out, _ = run
    heat = [a for a in alerts if a["scenario_id"] == "heat-stress-cluster"]
    assert heat, f"no heat alert; got {[(a['scenario_id'], a['site_names']) for a in alerts]}"
    a = heat[0]
    assert a["sites_affected"] == ["site-dxb"]
    assert a["severity"] in ("watch", "advisory", "warning")
    lo, hi = a["credible_interval"]
    assert lo <= a["score"] <= hi
    assert sum(a["drivers"].values()) == pytest.approx(100, abs=0.5)
    assert a["drivers"]["apparent_temp"] > 30
    assert a["action_owner"] in ("site supervisor", "MoHRE inspector")
    assert a["recommended_actions"] and a["hex_boundary"]["type"] == "Polygon"
    assert not any(x["scenario_id"] == "heat-stress-cluster" and x["sites_affected"] == ["site-auh"]
                   for x in alerts)
    df = pd.read_parquet(out / "active.parquet")
    assert len(df) == len(alerts) and df["id"].is_unique
    assert (out / "signal_traces.parquet").is_file() and (out / "fused_traces.parquet").is_file()


def test_dedupe_ids_are_stable_and_replay_ignores_future(world, run, tmp_path):
    alerts, _, cache = run
    again = A.generate_alerts(AS_OF + pd.Timedelta(minutes=15), CFG, out_dir=None,
                              cache_dir=cache, explain_dir=cache / "explain", panel_kwargs=world)
    assert {a["id"] for a in alerts} & {a["id"] for a in again}               # same window -> same id
    keys = [(a["scenario_id"], a["h3_cell"]) for a in alerts]
    assert len(keys) == len(set(keys))                                         # one per scenario+hex
    # Append extreme rows AFTER as_of: a replay at as_of must not see them.
    later = pd.date_range(AS_OF + pd.Timedelta(hours=1), periods=12, freq="h")
    future = dict(world)
    future["env"] = pd.concat([world["env"], pd.DataFrame(
        [{"site_id": s, "ts_utc": t, "apparent_temp": 60.0, "pm25": 500.0, "pm10": 900.0}
         for s in ("site-dxb", "site-auh") for t in later])], ignore_index=True)
    future["features"] = pd.concat([world["features"], pd.DataFrame(
        [{"site_id": s, "ts_utc": t, "scheduled_on_site": 150, "absence_rate_last_1h": 0.9,
          "heat_absence_rate_last_1h": 0.9, "respiratory_absence_rate_last_1h": 0.9}
         for s in ("site-dxb", "site-auh") for t in later])], ignore_index=True)
    future["events"] = pd.concat([world["events"], pd.DataFrame(
        [{"patient_id": "p1", "event_type": "encounter", "code": "x", "encounter_id": f"f{i}",
          "event_time": later[i % 12], "reason_code": "34095006"} for i in range(200)])],
        ignore_index=True)
    replay = A.generate_alerts(AS_OF, CFG, out_dir=None, cache_dir=tmp_path,
                                explain_dir=tmp_path / "explain", panel_kwargs=future)
    strip = lambda xs: sorted((a["id"], a["severity"], a["score"]) for a in xs)  # noqa: E731
    assert strip(replay) == strip(alerts)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(run, tmp_path, monkeypatch):
    _, out, _ = run
    monkeypatch.setenv("ALERTS_DATA_DIR", str(out))
    monkeypatch.setenv("ALERTS_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("WEBSITE_SITE_NAME", raising=False)
    api_dir = ROOT / "apps" / "api"
    spec = importlib.util.spec_from_file_location("alerts_router", api_dir / "routers" / "alerts.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(mod.router, prefix="/api/alerts")
    return TestClient(app), mod, tmp_path


def test_api_list_filter_detail_ack_refresh(client, monkeypatch):
    c, mod, state = client
    body = c.get("/api/alerts", params={"include_expired": True}).json()
    assert body["count"] >= 1 and body["timezone"] == "Asia/Dubai"
    a = body["alerts"][0]
    assert a["created_at"].endswith("+04:00") and "demo" not in a["id"]
    assert c.get("/api/alerts", params={"include_expired": True, "site_id": "nope"}).json()["count"] == 0
    assert c.get("/api/alerts", params={"severity": "bogus"}).status_code == 422
    # alerts in the synthetic world are from Aug 2026, so they're expired "now"
    assert c.get("/api/alerts").json()["count"] == 0

    d = c.get(f"/api/alerts/{a['id']}").json()["alert"]
    assert d["footprint"]["geometry"]["type"] == "Polygon"
    assert set(d["traces"]["signals"]) == set(a["drivers"]) | set(a["driver_details"])
    assert d["traces"]["fused"] and d["acks"] == []
    assert c.get("/api/alerts/does-not-exist").status_code == 404

    r = c.post(f"/api/alerts/{a['id']}/ack", json={"acknowledged_by": "Site supervisor", "notes": "breaks extended"})
    assert r.status_code == 201
    lines = (state / "acks.jsonl").read_text().splitlines()
    assert json.loads(lines[-1])["alert_id"] == a["id"]
    assert c.get(f"/api/alerts/{a['id']}").json()["alert"]["acks"][0]["notes"] == "breaks extended"
    assert c.post(f"/api/alerts/{a['id']}/ack", json={"acknowledged_by": ""}).status_code == 422

    assert c.post("/api/alerts/refresh").status_code == 503                   # not configured
    monkeypatch.setenv("ALERTS_REFRESH_SECRET", "s3cret")
    assert c.post("/api/alerts/refresh", headers={"X-Refresh-Secret": "wrong"}).status_code == 401
    assert c.post("/api/alerts/refresh", headers={"X-Refresh-Secret": "s3cret"}).json()["ok"]
