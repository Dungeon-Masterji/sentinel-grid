"""Backtest harness (packages/pipelines/backtest): zero lookahead, matching, metrics.

The lookahead tests are metamorphic as well as assertive: future data is filled with
absurd values, so if anything after as_of leaked into a computation, the result would
change. Offline: reuses the synthetic world from tests/test_fusion.py.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("prophet")
pytest.importorskip("pyod")

from packages.pipelines.backtest import events as EV  # noqa: E402
from packages.pipelines.backtest import history as H  # noqa: E402
from packages.pipelines.backtest import metrics as MX  # noqa: E402
from packages.pipelines.backtest import replay as R  # noqa: E402
from packages.pipelines.fusion import alerts as A  # noqa: E402
from tests.test_fusion import AS_OF, CFG, _world  # noqa: E402

FUTURE_DAYS = 3


def _with_future(world: dict, days: int = FUTURE_DAYS) -> dict:
    """The same world plus ``days`` of absurd future data after AS_OF."""
    w = {k: v.copy() for k, v in world.items()}
    last = AS_OF.floor("D")
    env_day = world["env"][world["env"]["ts_utc"] >= last - pd.Timedelta(days=1)]
    feat_day = world["features"][world["features"]["ts_utc"] >= last - pd.Timedelta(days=1)]
    envs, feats, evs = [w["env"]], [w["features"]], [w["events"]]
    for d in range(1, days + 1):
        shift = pd.Timedelta(days=d)
        e = env_day.assign(ts_utc=env_day["ts_utc"] + shift, apparent_temp=65.0, pm25=900.0, pm10=2000.0)
        envs.append(e[e["ts_utc"] > AS_OF])
        f = feat_day.assign(ts_utc=feat_day["ts_utc"] + shift, absence_rate_last_1h=0.9,
                            heat_absence_rate_last_1h=0.9, respiratory_absence_rate_last_1h=0.9)
        feats.append(f[f["ts_utc"] > AS_OF])
        t = AS_OF + pd.Timedelta(minutes=5) + shift
        evs.append(pd.DataFrame({"patient_id": "p0", "event_type": "encounter", "code": "x",
                                 "encounter_id": [f"future{d}-{i}" for i in range(500)],
                                 "event_time": t, "reason_code": "34095006"}))
    w["env"] = pd.concat(envs, ignore_index=True)
    w["features"] = pd.concat(feats, ignore_index=True)
    w["events"] = pd.concat(evs, ignore_index=True)
    return w


@pytest.fixture(scope="module")
def world():
    return _world()


# ---------------------------------------------------------------------------------------
# 1. the live pipeline: generate_alerts(as_of=T) never reads observations after T
# ---------------------------------------------------------------------------------------
def test_generate_alerts_never_reads_data_after_as_of(world, monkeypatch):
    seen = {}
    real_fit, real_detect = A.fit_all, A.detect

    def spy_fit(cfg, panel, as_of, **kw):
        seen["fit_max_ts"] = panel["ts_utc"].max()
        return real_fit(cfg, panel, as_of, **kw)

    def spy_detect(panel, cfg, as_of, **kw):
        seen["detect_max_ts"] = panel["ts_utc"].max()
        out = real_detect(panel, cfg, as_of, **kw)
        seen["scored_max_ts"] = out["ts_utc"].max()
        return out

    monkeypatch.setattr(A, "fit_all", spy_fit)
    monkeypatch.setattr(A, "detect", spy_detect)
    future = _with_future(world)
    assert future["env"]["ts_utc"].max() > AS_OF + pd.Timedelta(days=2)    # the trap is set
    with_future = A.generate_alerts(AS_OF, CFG, out_dir=None, cache_dir=None, explain_dir=None,
                                    panel_kwargs=future, llm=False)
    for k in ("fit_max_ts", "detect_max_ts", "scored_max_ts"):
        assert seen[k] <= AS_OF, f"{k} = {seen[k]} > as_of {AS_OF}"
    for a in with_future:
        assert pd.Timestamp(a["peak_at"]) <= AS_OF

    # metamorphic: absurd future values change nothing
    monkeypatch.setattr(A, "fit_all", real_fit)
    monkeypatch.setattr(A, "detect", real_detect)
    clean = A.generate_alerts(AS_OF, CFG, out_dir=None, cache_dir=None, explain_dir=None,
                              panel_kwargs=world, llm=False)
    key = lambda al: sorted((a["id"], a["severity"], round(a["score"], 6)) for a in al)  # noqa: E731
    assert key(with_future) == key(clean)


# ---------------------------------------------------------------------------------------
# 2. the replay: assert_no_lookahead + a corrupted history file must fail loudly
# ---------------------------------------------------------------------------------------
def test_assert_no_lookahead_is_strict():
    t = pd.Timestamp("2024-06-16T10:00Z")
    ok = pd.DataFrame({"ts_utc": [t - pd.Timedelta(hours=1)], "observed_at": [t - pd.Timedelta(hours=1)]})
    R.assert_no_lookahead(ok, t)
    for col in ("ts_utc", "observed_at"):
        bad = ok.copy()
        bad[col] = t                                     # equal is already lookahead
        with pytest.raises(AssertionError, match="zero-lookahead"):
            R.assert_no_lookahead(bad, t)


@pytest.fixture(scope="module")
def history_dir(world, tmp_path_factory):
    d = tmp_path_factory.mktemp("bt_history")
    lo, hi = world["env"]["ts_utc"].min(), AS_OF
    H.pull_history(lo.date(), hi.date(), sites=world["sites"], scenarios_yaml=CFG, out_dir=d,
                   env=world["env"], station_env=world["env"], features=world["features"],
                   events=world["events"], patients=world["patients"])
    return d


def test_history_files_carry_provenance_and_clinical_lag(history_dir, world):
    panel, units = H.load_history(world["env"]["ts_utc"].min(), AS_OF, history_dir)
    assert set(H.HISTORY_COLUMNS) <= set(panel.columns)
    assert (panel["available_at"] == panel["ts_utc"] + H.PUBLISH_LATENCY).all()
    clin = panel["pillar"] == "clin"
    assert clin.any()
    assert (panel.loc[clin, "ts_utc"] - panel.loc[clin, "observed_at"] == H.CLINICAL_LAG).all()
    assert (panel.loc[~clin, "ts_utc"] == panel.loc[~clin, "observed_at"]).all()
    # idempotent: a second call writes nothing
    before = {p: p.stat().st_mtime for p in history_dir.glob("20*.parquet")}
    H.pull_history(world["env"]["ts_utc"].min().date(), AS_OF.date(), out_dir=history_dir,
                   sites=world["sites"], scenarios_yaml=CFG)
    assert {p: p.stat().st_mtime for p in history_dir.glob("20*.parquet")} == before


REPLAY_START = AS_OF.floor("D") - pd.Timedelta(days=1)
REPLAY_END = AS_OF.floor("D") + pd.Timedelta(days=1)     # covers the injected heat spike


def test_replay_runs_clean_and_ignores_the_future(history_dir, world, tmp_path):
    panel, units = H.load_history(world["env"]["ts_utc"].min(), AS_OF, history_dir)
    r = R.replay(REPLAY_START, REPLAY_END, CFG, panel=panel, units=units, workers=1)
    assert len(r.fused) > 0
    assert len(r.alerts) > 0 and "heat-stress-cluster" in set(r.alerts["scenario_id"])
    assert set(r.alerts.columns) >= {"id", "first_seen", "severity_first", "matched"}
    assert ((r.alerts["first_seen"] >= REPLAY_START) & (r.alerts["first_seen"] < REPLAY_END)).all()
    assert (r.alerts["first_seen"].dt.minute % 15 == 0).all()            # 15-min cadence

    # metamorphic: append absurd rows that only become available after the window
    fut = panel[panel["ts_utc"] >= panel["ts_utc"].max() - pd.Timedelta(days=1)].copy()
    shift = REPLAY_END - fut["ts_utc"].min() + pd.Timedelta(hours=2)     # all after the window
    fut["ts_utc"] += shift
    fut["observed_at"] += shift
    fut["available_at"] += shift
    fut["value"] = fut["value"] * 50 + 100
    r2 = R.replay(REPLAY_START, REPLAY_END, CFG, panel=pd.concat([panel, fut], ignore_index=True),
                  units=units, workers=1)
    cols = ["id", "first_seen", "severity_first", "severity_max"]
    pd.testing.assert_frame_equal(r.alerts[cols].reset_index(drop=True),
                                  r2.alerts[cols].reset_index(drop=True))


def test_replay_fails_loudly_on_corrupted_future_timestamp(history_dir, world, tmp_path):
    """A history file whose row CLAIMS to be published in the past but carries a future
    observation time (a corrupted / mislabelled file) must abort the replay."""
    import shutil
    bad_dir = tmp_path / "corrupt"
    shutil.copytree(history_dir, bad_dir)
    f = sorted(bad_dir.glob("20*.parquet"))[-1]
    df = pd.read_parquet(f)
    i = df.index[df["available_at"] <= REPLAY_START][-1]
    df.loc[i, "ts_utc"] = pd.Timestamp("2030-01-01T00:00Z")    # observation from the future...
    df.loc[i, "observed_at"] = pd.Timestamp("2030-01-01T00:00Z")
    df.to_parquet(f)                                             # ...but available_at untouched
    with pytest.raises(AssertionError, match="zero-lookahead violated"):
        R.replay(REPLAY_START, REPLAY_END, CFG, history_dir=bad_dir, workers=1)


# ---------------------------------------------------------------------------------------
# 3. ground truth, matching, metrics
# ---------------------------------------------------------------------------------------
def test_ground_truth_yaml_is_complete():
    ev = EV.load_events()
    assert ev["source_url"].str.startswith("http").all()
    by = ev.set_index("id")
    assert by.loc["heat-2024-06-15", "start"] == pd.Timestamp("2024-06-14T20:00Z")
    assert by.loc["flood-2024-04-16", "event_type"] == "flood"
    assert by.loc["ramadan-2024", "start"] == pd.Timestamp("2024-03-10T20:00Z")
    dust24 = ev[(ev["event_type"] == "dust_storm") & (ev["start"].dt.year == 2024)]
    assert len(dust24) >= 3
    assert (ev["end"] > ev["start"]).all()


def _toy():
    t0 = pd.Timestamp("2024-06-15T00:00Z")
    events = pd.DataFrame([
        {"id": "heat", "event_type": "heatwave", "start": t0, "end": t0 + pd.Timedelta(days=2),
         "peak": t0, "hexes": ["h1", "h2"], "scenarios": ["heat-stress-cluster"]},
        {"id": "dust", "event_type": "dust_storm", "start": t0 + pd.Timedelta(days=1),
         "end": t0 + pd.Timedelta(days=1, hours=8), "peak": t0, "hexes": ["h1"],
         "scenarios": ["respiratory-dust-event"]},
        {"id": "flood", "event_type": "flood", "start": t0 + pd.Timedelta(days=30),
         "end": t0 + pd.Timedelta(days=31), "peak": t0, "hexes": ["h1"],
         "scenarios": ["respiratory-dust-event"]},
    ])
    alerts = pd.DataFrame([
        # 10 h early warning for the heatwave, then an escalation
        {"id": "a1", "scenario_id": "heat-stress-cluster", "h3_cell": "h1",
         "first_seen": t0 - pd.Timedelta(hours=10), "severity_first": "watch", "severity_max": "advisory"},
        {"id": "a2", "scenario_id": "heat-stress-cluster", "h3_cell": "h2",
         "first_seen": t0 + pd.Timedelta(hours=20), "severity_first": "warning", "severity_max": "warning"},
        # dust caught 3 h AFTER it started (negative lead)
        {"id": "a3", "scenario_id": "respiratory-dust-event", "h3_cell": "h1",
         "first_seen": t0 + pd.Timedelta(days=1, hours=3), "severity_first": "watch", "severity_max": "watch"},
        # false alarms: wrong place / far from any event
        {"id": "a4", "scenario_id": "heat-stress-cluster", "h3_cell": "h9",
         "first_seen": t0, "severity_first": "watch", "severity_max": "watch"},
        {"id": "a5", "scenario_id": "respiratory-dust-event", "h3_cell": "h1",
         "first_seen": t0 + pd.Timedelta(days=10), "severity_first": "watch", "severity_max": "watch"},
    ])
    return alerts, events


def test_matching_lead_time_and_severities():
    alerts, events = _toy()
    a, e = EV.match(alerts, events, lead_window_hours=72, grace_hours=12)
    e = e.set_index("id")
    assert e.loc["heat", "detected"] and e.loc["heat", "lead_time_hours"] == 10.0
    assert e.loc["heat", "alert_severity_at_first_hit"] == "watch"
    assert e.loc["heat", "peak_severity_during_event"] == "warning"
    assert e.loc["dust", "lead_time_hours"] == -3.0            # late = negative
    assert not e.loc["flood", "detected"]
    assert a.set_index("id")["matched"].to_dict() == {"a1": True, "a2": True, "a3": True,
                                                      "a4": False, "a5": False}


def test_scorecard_numbers():
    alerts, events = _toy()
    a, e = EV.match(alerts, events)
    card = MX.produce_scorecard(a, e, weeks=2.0, scenarios=["heat-stress-cluster", "respiratory-dust-event"])
    o = card["overall"]
    assert o["sensitivity"] == round(2 / 3, 3) and o["detected"] == 2
    assert o["ppv"] == 0.6 and o["false_alarms"] == 2 and o["false_alarms_per_week"] == 1.0
    assert o["mean_lead_time_hours"] == 3.5                      # (10 + -3) / 2
    cm = card["coverage_matrix"]
    row = dict(zip(cm["event_types"], cm["values"][cm["scenarios"].index("respiratory-dust-event")]))
    # coverage ignores the scenario map: the dust alert inside the heatwave counts there too
    assert row["dust_storm"] == 1.0 and row["flood"] == 0.0 and row["heatwave"] == 1.0
    assert row["infectious_cluster"] is None                     # no events of that type


def test_overlapping_events_scored_independently():
    alerts, events = _toy()
    combo = events.iloc[[0]].assign(id="combo", event_type="heat_wave_combined_with_dust",
                                    scenarios=[["heat-stress-cluster", "respiratory-dust-event"]])
    _, e = EV.match(alerts, pd.concat([events, combo], ignore_index=True))
    assert e.set_index("id").loc[["heat", "dust", "combo"], "detected"].all()
    hc = MX.coverage_matrix(alerts, pd.concat([events, combo], ignore_index=True),
                            ["heat-stress-cluster", "respiratory-dust-event"])
    assert hc.loc["heat-stress-cluster", "heat_wave_combined_with_dust"] == 1.0
    assert hc.loc["respiratory-dust-event", "heat_wave_combined_with_dust"] == 1.0


def test_calibration_and_brier():
    t0 = pd.Timestamp("2024-06-15T00:00Z")
    events = pd.DataFrame([{"id": "e", "event_type": "heatwave", "start": t0,
                            "end": t0 + pd.Timedelta(hours=9), "hexes": ["h1"],
                            "scenarios": ["heat-stress-cluster"]}])
    ts = pd.date_range(t0 - pd.Timedelta(hours=10), periods=20, freq="h")
    fused = pd.DataFrame({"scenario_id": "heat-stress-cluster", "unit_h3": "h1", "ts_utc": ts,
                          "mean": [0.05] * 10 + [0.95] * 10, "eligible": True})
    c = MX.calibration(fused, events, bins=10)
    assert c["positives"] == 10 and c["n"] == 20
    assert c["brier"] == pytest.approx(0.0025, abs=1e-4)
    assert c["brier_climatology"] == pytest.approx(0.25) and c["brier_skill"] > 0.98
    top = [b for b in c["bins"] if b["n"]]
    assert [b["observed_frequency"] for b in top] == [0.0, 1.0]


def test_chance_baseline_and_tiers():
    alerts, events = _toy()
    alerts = alerts.assign(first_seen_watch=alerts["first_seen"],
                           first_seen_advisory=[alerts["first_seen"][0] + pd.Timedelta(hours=5), pd.NaT,
                                                pd.NaT, pd.NaT, pd.NaT],
                           first_seen_warning=[pd.NaT, alerts["first_seen"][1], pd.NaT, pd.NaT, pd.NaT])
    start, end = pd.Timestamp("2024-01-01T00:00Z"), pd.Timestamp("2025-01-01T00:00Z")
    tb = MX.tier_blocks(alerts, events, 52.0, start, end, chance_n=50)
    assert tb["watch"]["alerts"] == 5 and tb["advisory"]["alerts"] == 1 and tb["warning"]["alerts"] == 1
    # advisory is timed from when the alert REACHED advisory: 10 h early -> 5 h early
    assert tb["advisory"]["mean_lead_time_hours"] == 5.0
    for mode in ("uniform", "seasonal"):
        ch = tb["watch"][f"chance_{mode}"]
        assert ch["n"] == 50 and 0 <= ch["sensitivity_mean"] <= 1
    # alerts sitting exactly on events beat randomly timed copies of themselves
    assert tb["watch"]["ppv_lift"] > 1


# ---------------------------------------------------------------------------------------
# 4. API: /api/backtest/*
# ---------------------------------------------------------------------------------------
@pytest.fixture
def backtest_client(tmp_path, monkeypatch):
    import importlib.util
    import json
    from pathlib import Path
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    ev = {"id": "heat-x", "label": "Heat", "event_type": "heatwave", "start": "2024-06-15T00:00:00+00:00",
          "end": "2024-06-16T00:00:00+00:00", "peak": "2024-06-15T10:00:00+00:00",
          "first_alert_time": "2024-06-14T20:00:00+00:00", "detected": True, "lead_time_hours": 4.0,
          "alerts": [{"id": "a", "scenario_id": "heat-stress-cluster", "h3_cell": "h1",
                      "first_seen": "2024-06-14T20:00:00+00:00", "severity_first": "watch",
                      "severity_max": "advisory", "score_max": 0.6, "expected": True}]}
    (tmp_path / "scorecard.json").write_text(json.dumps({"overall": {"sensitivity": 1.0}}))
    (tmp_path / "events.json").write_text(json.dumps([ev]))
    (tmp_path / "calibration.json").write_text(json.dumps({"overall": {"bins": [], "brier": 0.1}}))
    pd.DataFrame({"event_id": "heat-x", "ts_utc": pd.to_datetime(["2024-06-15T00:00Z"] * 2),
                  "series": ["apparent_temp", "fused:heat-stress-cluster"], "value": [45.0, 0.55]}) \
        .to_parquet(tmp_path / "event_traces.parquet")
    monkeypatch.setenv("BACKTEST_DATA_DIR", str(tmp_path))
    api = Path(__file__).resolve().parents[1] / "apps" / "api" / "routers" / "backtest.py"
    spec = importlib.util.spec_from_file_location("backtest_router", api)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    app = FastAPI()
    app.include_router(mod.router, prefix="/api/backtest")
    return TestClient(app)


def test_backtest_endpoints(backtest_client):
    c = backtest_client
    assert c.get("/api/backtest/scorecard").json()["overall"]["sensitivity"] == 1.0
    evs = c.get("/api/backtest/events").json()["events"]
    assert evs[0]["start"] == "2024-06-15T04:00:00+04:00" and "alerts" not in evs[0]
    assert c.get("/api/backtest/calibration").json()["overall"]["brier"] == 0.1
    r = c.get("/api/backtest/replay/heat-x").json()
    assert r["hours"][0]["signals"] == {"apparent_temp": 45.0}
    assert r["hours"][0]["fused"] == {"heat-stress-cluster": 0.55}
    assert r["event"]["alerts"][0]["first_seen"].endswith("+04:00")
    assert c.get("/api/backtest/replay/nope").status_code == 404
