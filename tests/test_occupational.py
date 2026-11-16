"""Tests for the occupational pillar (synthetic workforce, env join, features, API).

Offline: env signals are synthetic frames, no network calls.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from packages.pipelines.synth import env_join, workforce as wfm
from packages.pipelines.synth.occupational_features import build_feature_frame, write_parquet
from packages.pipelines.synth.sites import MIX_YAML, SITES_YAML, h3_mismatches, load_sites, load_yaml

ROOT = Path(__file__).resolve().parents[1]
SUBSET = ["dubai-creek-harbour", "jebel-ali-port", "dubai-south-logistics"]  # outdoor/mixed/indoor_cool
START, END = date(2026, 8, 3), date(2026, 8, 16)


def _env(sites: pd.DataFrame, start: date, end: date, *, at: float, pm25: float, pm10: float):
    ts = pd.date_range(pd.Timestamp(start, tz="Asia/Dubai").tz_convert("UTC"),
                       pd.Timestamp(end + timedelta(days=2), tz="Asia/Dubai").tz_convert("UTC"),
                       freq="h")
    return pd.DataFrame([{"site_id": s, "ts_utc": t, "apparent_temp": at, "pm25": pm25, "pm10": pm10}
                         for s in sites["site_id"] for t in ts])


@pytest.fixture(scope="module")
def sites():
    s = load_sites(SITES_YAML)
    return s[s["site_id"].isin(SUBSET)].reset_index(drop=True)


@pytest.fixture(scope="module")
def roster(sites):
    return wfm.generate_workforce(sites, MIX_YAML, seed=42)


@pytest.fixture(scope="module")
def cool_log(sites, roster):
    env = _env(sites, START, END, at=28, pm25=20, pm10=40)
    return wfm.generate_shift_log(roster, START, END, env, seed=42)


@pytest.fixture(scope="module")
def hot_log(sites, roster):
    env = _env(sites, START, END, at=50, pm25=140, pm10=300)
    return wfm.generate_shift_log(roster, START, END, env, seed=42)


# --- sites --------------------------------------------------------------------

def test_sites_yaml_has_15_valid_sites_with_current_h3():
    s = load_sites(SITES_YAML)
    assert len(s) == 15
    assert s["worker_count"].between(200, 2000).all()
    assert h3_mismatches(s) == []


# --- workforce ----------------------------------------------------------------

def test_workforce_reproducible_and_shaped(sites, roster):
    again = wfm.generate_workforce(sites, MIX_YAML, seed=42)
    pd.testing.assert_frame_equal(roster, again)
    other = wfm.generate_workforce(sites, MIX_YAML, seed=7)
    assert not roster["age"].equals(other["age"])

    assert list(roster.columns[:8]) == ["worker_id", "site_id", "nationality", "role", "age",
                                        "gender", "ppe_compliance_baseline", "start_date"]
    counts = roster.groupby("site_id", observed=True).size()
    assert counts.to_dict() == sites.set_index("site_id")["worker_count"].to_dict()
    assert roster["worker_id"].is_unique
    assert roster["age"].between(18, 60).all()
    assert 30 <= roster["age"].mean() <= 34
    assert roster["ppe_compliance_baseline"].between(0, 1).all()
    # No personal names anywhere: ids are opaque site-scoped counters.
    assert roster["worker_id"].str.fullmatch(r"[a-z0-9-]+-\d{5}").all()
    assert not any("name" in c for c in roster.columns)


def test_construction_nationality_mix_matches_brief(sites, roster):
    c = roster[roster["sector"] == "construction"]["nationality"].value_counts(normalize=True)
    assert abs(c["Indian"] - 0.35) < 0.04
    assert abs(c["Pakistani"] - 0.22) < 0.04
    # safety officers comply more than laborers (role bias)
    by_role = roster.groupby("role", observed=True)["ppe_compliance_baseline"].mean()
    assert by_role["safety_officer"] > by_role["laborer"]


# --- shift log ----------------------------------------------------------------

def _rate(log, site, reason=None):
    g = log[log["site_id"] == site]
    r = g["absence_reason"].astype(str)
    return (r != "none").mean() if reason is None else (r == reason).mean()


def test_shift_log_columns_and_reproducible(sites, roster, cool_log):
    assert list(cool_log.columns) == wfm.SHIFT_LOG_COLUMNS
    env = _env(sites, START, END, at=28, pm25=20, pm10=40)
    pd.testing.assert_frame_equal(cool_log, wfm.generate_shift_log(roster, START, END, env, seed=42))
    assert str(cool_log["shift_start_utc"].dt.tz) == "UTC"


def test_extending_range_keeps_earlier_days(sites, roster, cool_log):
    env = _env(sites, START, END + timedelta(days=3), at=28, pm25=20, pm10=40)
    longer = wfm.generate_shift_log(roster, START, END + timedelta(days=3), env, seed=42)
    head = longer[longer["shift_date"] <= END].reset_index(drop=True)
    pd.testing.assert_frame_equal(head, cool_log.reset_index(drop=True))


def test_absence_responds_to_environment(cool_log, hot_log):
    # Cool, clean air: only the ~2% non-environmental baseline.
    for s in SUBSET:
        assert _rate(cool_log, s, "heat") == 0
        assert _rate(cool_log, s, "respiratory") == 0
        assert 0.01 < _rate(cool_log, s) < 0.03
    # Hot + dusty: heat absence at outdoor/mixed sites, none at the indoor_cool site.
    assert _rate(hot_log, "dubai-creek-harbour", "heat") > 0.03
    assert _rate(hot_log, "jebel-ali-port", "heat") > 0.03
    assert _rate(hot_log, "dubai-south-logistics", "heat") == 0
    for s in SUBSET:
        assert _rate(hot_log, s, "respiratory") > 0.01
        assert _rate(hot_log, s) > _rate(cool_log, s)


def test_ppe_degrades_above_42c(cool_log, hot_log):
    def ppe(log, s):
        return log.loc[log["site_id"] == s, "ppe_compliance_actual"].mean()
    # outdoor: -0.15 * (50-42)/10 = -0.12
    assert ppe(cool_log, "dubai-creek-harbour") - ppe(hot_log, "dubai-creek-harbour") == \
        pytest.approx(0.12, abs=0.02)
    # indoor_cool is climate-controlled
    assert ppe(cool_log, "dubai-south-logistics") == pytest.approx(
        ppe(hot_log, "dubai-south-logistics"), abs=0.01)
    assert hot_log["ppe_compliance_actual"].dropna().between(0, 1).all()
    absent = hot_log["absence_reason"].astype(str) != "none"
    assert hot_log.loc[absent, "ppe_compliance_actual"].isna().all()
    assert (hot_log.loc[absent, "worked_hours"] == 0).all()


def test_absence_probabilities_follow_brief():
    model = load_yaml(MIX_YAML)["absence_model"]
    ph, pr = wfm.absence_probabilities([38, 48], [20, 20], [40, 40], True, model)
    assert ph[0] == 0                                            # gated: no excess, no heat absence
    logit = np.log(0.02 / 0.98)
    assert ph[1] == pytest.approx(1 / (1 + np.exp(-(logit + 0.08 * 10))))
    assert (pr == 0).all()
    ph, pr = wfm.absence_probabilities([30], [155], [140], False, model)
    assert ph[0] == 0                                            # heat only applies outdoor/mixed
    assert pr[0] == pytest.approx(1 / (1 + np.exp(-(np.log(0.01 / 0.99) + 0.04 * 10 + 0.02 * 2))))


def test_midday_break_cuts_scheduled_hours(sites, roster, cool_log):
    # 15 Jun – 15 Sep: outdoor 06:00–18:00 shift loses 12:30–15:00.
    creek = cool_log[(cool_log["site_id"] == "dubai-creek-harbour")]
    assert set(creek.loc[creek["shift_type"] == "morning", "scheduled_hours"]) == {9.5}
    assert set(creek.loc[creek["shift_type"] == "night", "scheduled_hours"]) == {12.0}
    indoor = cool_log[cool_log["site_id"] == "dubai-south-logistics"]
    assert set(indoor["scheduled_hours"]) == {8.0}


# --- rollup -------------------------------------------------------------------

def test_daily_rollup(hot_log):
    r = wfm.daily_site_rollup(hot_log)
    assert list(r.columns) == wfm.ROLLUP_COLUMNS
    assert len(r) == len(SUBSET) * ((END - START).days + 1)
    one = r.iloc[0]
    g = hot_log[(hot_log["site_id"] == one["site_id"]) & (hot_log["shift_date"] == one["date"])]
    assert one["scheduled_count"] == len(g)
    assert one["absence_rate"] == pytest.approx((g["absence_reason"].astype(str) != "none").mean(), abs=1e-4)
    assert one["peak_absence_hour"] in {6, 14, 18, 22}
    assert (r["absence_rate"] >= r["heat_absence_rate"] + r["respiratory_absence_rate"] - 1e-9).all()


# --- env join -----------------------------------------------------------------

def test_merge_hourly_is_idempotent():
    t = pd.date_range("2026-08-01", periods=3, freq="h", tz="UTC")
    base = pd.DataFrame({c: np.nan for c in env_join.COLUMNS}, index=range(3))
    base["site_id"], base["ts_utc"], base["apparent_temp"] = "a", t, [30.0, 31.0, 32.0]
    upd = base.iloc[1:].copy()
    upd["apparent_temp"] = [41.0, 42.0]
    once = env_join.merge_hourly(base, upd)
    twice = env_join.merge_hourly(once, upd)
    pd.testing.assert_frame_equal(once, twice)
    assert once["apparent_temp"].tolist() == [30.0, 41.0, 42.0]


def test_aqi_to_concentration():
    assert env_join.aqi_to_concentration(50, "pm25") == pytest.approx(9.0)
    assert env_join.aqi_to_concentration(100, "pm10") == pytest.approx(154.0)
    assert env_join.aqi_to_concentration(None, "pm25") is None


# --- features + API -------------------------------------------------------------

@pytest.fixture(scope="module")
def api_bundle(tmp_path_factory, sites, roster, hot_log):
    d = tmp_path_factory.mktemp("occ")
    env = _env(sites, START, END, at=50, pm25=140, pm10=300)
    roll = wfm.daily_site_rollup(hot_log)
    feats = build_feature_frame(env, roll, roster, hot_log, out_path=d / "features.parquet")
    write_parquet(sites.assign(roster_size=sites["worker_count"]), d / "sites.parquet")
    write_parquet(roll, d / "daily_site_rollup.parquet")
    return d, feats


def test_feature_frame(api_bundle):
    _, f = api_bundle
    assert {"apparent_temp", "pm25", "pm10", "workers_on_site", "absence_rate_last_1h",
            "ppe_compliance", "heat_exposure", "shift_type"} <= set(f.columns)
    assert not f.duplicated(["site_id", "ts_utc"]).any()
    on = f[f["scheduled_on_site"] > 0]
    assert (on["workers_on_site"] <= on["scheduled_on_site"]).all()
    assert on["absence_rate_last_1h"].between(0, 1).all()
    # day_only patterns aside, 24/7 sites always have someone rostered once shifts begin
    jeb = f[(f["site_id"] == "jebel-ali-port") & (f["ts_utc"] >= pd.Timestamp("2026-08-04", tz="UTC"))
            & (f["ts_utc"] < pd.Timestamp("2026-08-16", tz="UTC"))]
    assert (jeb["scheduled_on_site"] > 0).all()


@pytest.fixture(scope="module")
def client(api_bundle):
    d, _ = api_bundle
    import os
    os.environ["OCCUPATIONAL_DATA_DIR"] = str(d)
    api_dir = ROOT / "apps" / "api"
    sys.path.insert(0, str(api_dir))
    try:
        spec = importlib.util.spec_from_file_location("occ_router", api_dir / "routers" / "occupational.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(api_dir))
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(mod.router, prefix="/api/signals/occupational")
    yield TestClient(app)
    os.environ.pop("OCCUPATIONAL_DATA_DIR", None)


def test_api_sites(client):
    r = client.get("/api/signals/occupational/sites")
    assert r.status_code == 200
    body = r.json()
    assert body["synthetic"] is True and body["timezone"] == "Asia/Dubai"
    assert body["totals"]["sites"] == 3
    tiers = {s["site_id"]: s["risk_tier"] for s in body["sites"]}
    # 50 °C all day: mean apparent temp > 42 → elevated everywhere
    assert set(tiers.values()) == {"red"}
    assert body["totals"]["elevated_sites"] == 3
    s = body["sites"][0]
    assert s["observed_at"].endswith("+04:00")
    assert len(s["worked_hours_7d"]) == 7


def test_api_detail_and_rollup(client):
    r = client.get("/api/signals/occupational/sites/jebel-ali-port")
    assert r.status_code == 200
    d = r.json()
    assert len(d["last_24h"]) == 24 and d["last_24h"][-1]["ts"].endswith("+04:00")
    assert client.get("/api/signals/occupational/sites/nope").status_code == 404

    r = client.get("/api/signals/occupational/rollup",
                   params={"since": "2026-08-10", "until": "2026-08-11", "site_id": "jebel-ali-port"})
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert [x["date"] for x in rows] == ["2026-08-10", "2026-08-11"]
    assert client.get("/api/signals/occupational/rollup",
                      params={"since": "2026-08-12", "until": "2026-08-10"}).status_code == 422
