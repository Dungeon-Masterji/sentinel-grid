"""Tri-signal alert generator.

    python -m packages.pipelines.fusion.alerts                       # as of now (UTC)
    python -m packages.pipelines.fusion.alerts --as-of 2026-08-20T10:00:00Z   # replay
    python -m packages.pipelines.fusion.alerts --mode sampled         # full PyMC posterior

generate_alerts(as_of_time, scenarios_yaml):
  1. build the signal panel up to as_of_time (signals.py; suppression applied there)
  2. score each unique signal once: Prophet baseline + PyOD ensemble (detect.py)
  3. per scenario per site hex: Bayesian fusion -> P(firing) mean + 90% CI (combine.py)
  4. threshold into watch / advisory / warning; ineligible hexes never alert:
       <5 clinical encounters in the catchment AND <3 workers AND no env station
  5. dedupe: one alert per scenario per hex per 6-hour UTC window. The id is a
     deterministic uuid5 of (scenario, hex, window), so 15-minute re-runs update the
     same alert instead of creating new ones, and acknowledgements stick.
  6. explain: per scenario, a LightGBM surrogate of the fused score is explained with
     TreeSHAP at each alert's peak hour (packages/pipelines/explain/shap_runner.py).
     `drivers` = SHAP shares; `drivers_prior` = the static fusion-weight shares. If the
     surrogate cannot be fitted (sparse hexes), `drivers` falls back to the prior and
     `drivers_method` says so: an alert never ships without drivers.
  7. narrate (Chunk 5, packages/pipelines/explain/llm.py): Groq LLM English narration,
     Arabic translation and a structured recommended action per alert, cached in
     data/processed/explain/narration_cache.sqlite. Template fallback (and
     explanation_source = "template_fallback") when the LLM is off, rate-limited,
     over budget, or returns something that fails validation.
  8. write data/processed/alerts/{active,signal_traces,fused_traces}.parquet + meta.json
  9. route: POST every alert not yet routed at its current severity to the n8n webhook
     ($N8N_WEBHOOK_URL; packages/pipelines/ops/notify.py), logged in alerts/sends.jsonl.
     Unset -> skipped and logged.

Only the CLI entry point reads the clock; every function takes time from its caller.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from packages.pipelines.fusion.baselines import CACHE_DIR  # noqa: E402
from packages.pipelines.fusion.combine import contributions, fuse  # noqa: E402
from packages.pipelines.fusion.config import SCENARIOS_YAML, SEVERITIES, load_scenarios  # noqa: E402
from packages.pipelines.fusion.detect import detect, fit_all  # noqa: E402
from packages.pipelines.fusion.signals import PROCESSED, build_signal_panel  # noqa: E402
from packages.pipelines.fusion.spatial import hex_boundary_geojson  # noqa: E402
from packages.pipelines.explain.groq_client import NarrationCache  # noqa: E402
from packages.pipelines.explain.llm import narrate_alerts  # noqa: E402
from packages.pipelines.explain.narrate import narrate_alert_template  # noqa: E402
from packages.pipelines.explain.shap_runner import (EXPLAIN_DIR, explain_config,  # noqa: E402
                                                    fit_scenario_surrogates,
                                                    per_alert_contributions)

log = logging.getLogger("sentinel.fusion.alerts")
ALERTS_DIR = PROCESSED / "alerts"
ALERT_NS = uuid.UUID("6f1d2c3e-9a4b-4c5d-8e7f-0a1b2c3d4e5f")
TRACE_HOURS = 48
NOTICE = ("Fused from synthetic clinical (Synthea) and synthetic workforce data plus real "
          "environmental observations. Decision support only.")


def _severity(mean: float, th: dict) -> str | None:
    if not np.isfinite(mean):
        return None
    tier = None
    for s in SEVERITIES:
        if mean >= th[s]:
            tier = s
    return tier


def _source_hex(unit: pd.Series, pillar: str) -> str:
    return {"env": unit["env_h3"], "occ": unit["h3"], "clin": unit["clinical_h3"]}[pillar]


def unit_signal_frame(scored: pd.DataFrame, units: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Per (unit h3, ts) rows with, for each signal, value/baseline/z/score read from the
    pillar's source hex, plus eligibility inputs. Columns are '<signal>__<field>'."""
    sig_meta = cfg["signals"]
    idx = scored.set_index(["signal", "h3", "ts_utc"]).sort_index()
    frames = []
    for _, u in units.iterrows():
        cols = {}
        for sig, meta in sig_meta.items():
            src = _source_hex(u, meta["pillar"])
            try:
                sub = idx.loc[(sig, src)]
            except KeyError:
                continue
            for f in ("value", "baseline", "z", "score", "n_records", "suppressed"):
                cols[f"{sig}__{f}"] = sub[f]
        if not cols:
            continue
        f = pd.DataFrame(cols)
        f.index.name = "ts_utc"
        f = f.reset_index()
        f.insert(0, "unit_h3", u["h3"])
        frames.append(f)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _eligible(frame: pd.DataFrame, cfg: dict) -> np.ndarray:
    """Suppression wins: a hex-hour is eligible only if it has >= min clinical encounters
    in its catchment OR >= min workers OR an environmental station reading."""
    sp = cfg["spatial"]
    sig = cfg["signals"]

    def _any(pillar, test):
        cols = [s for s, m in sig.items() if m["pillar"] == pillar]
        ok = np.zeros(len(frame), dtype=bool)
        for s in cols:
            c = f"{s}__n_records"
            if c in frame:
                ok |= test(frame[c].astype(float).to_numpy())
        return ok

    clin_ok = _any("clin", lambda n: np.nan_to_num(n) >= int(sp.get("min_clinical_for_alert", 5)))
    occ_ok = _any("occ", lambda n: np.nan_to_num(n) >= int(sp.get("min_workers_for_alert", 3)))
    env_ok = _any("env", lambda n: np.nan_to_num(n) >= 1)
    return clin_ok | occ_ok | env_ok


def fuse_all(frame: pd.DataFrame, cfg: dict, *, mode: str = "map",
             rows_for_sampled: np.ndarray | None = None) -> pd.DataFrame:
    """Per scenario fused posterior for each row of ``frame`` (mode=map) or only for
    ``rows_for_sampled`` (mode=sampled; other rows stay NaN)."""
    out = []
    prior = tuple(cfg.get("fusion", {}).get("prior", [2, 2]))
    eligible = _eligible(frame, cfg)
    min_cov = float(cfg.get("fusion", {}).get("min_coverage", 0.4))
    for sc in cfg["scenarios"]:
        weights = {s["name"]: float(s["weight"]) for s in sc["signals"]}
        S = pd.DataFrame({k: frame[f"{k}__score"] if f"{k}__score" in frame else np.nan
                          for k in weights}, index=frame.index)
        res = pd.DataFrame(index=frame.index, columns=["mean", "ci_low", "ci_high", "coverage", "sigma"],
                           dtype=float)
        if mode == "map":
            res[:] = fuse(S, weights, mode="map", prior=prior).to_numpy()
        else:
            rows = rows_for_sampled if rows_for_sampled is not None else np.arange(len(S))
            r = fuse(S, weights, mode="sampled", prior=prior, rows=rows)
            res.loc[r.index] = r.to_numpy()
        res["scenario_id"] = sc["id"]
        res["unit_h3"] = frame["unit_h3"].to_numpy()
        res["ts_utc"] = frame["ts_utc"].to_numpy()
        ok = eligible & (res["coverage"].to_numpy(float) >= min_cov)
        res["eligible"] = ok
        res["severity"] = [(_severity(m, sc["thresholds"]) if e else None)
                           for m, e in zip(res["mean"].to_numpy(float), ok)]
        out.append(res.reset_index(drop=True))
    return pd.concat(out, ignore_index=True)


def _window_bounds(as_of: pd.Timestamp, hours: int):
    w = as_of.floor(f"{hours}h")
    return [(w - pd.Timedelta(hours=hours), w), (w, w + pd.Timedelta(hours=hours))]


def _iso(ts) -> str:
    return pd.Timestamp(ts).tz_convert("UTC").isoformat().replace("+00:00", "Z")


ONSET_Z = 1.0          # a driver counts as "rising" while its residual z stays above this


def _onsets(fr: pd.DataFrame, cell: str, peak_ts: pd.Timestamp, signals: list[str]) -> dict:
    """Start of the unbroken run of hours (within 24 h, ending at the peak) in which
    each signal's z-score stayed above ONSET_Z; None if it was not elevated at the peak."""
    try:
        u = fr.loc[cell]
    except KeyError:
        return {}
    u = u[(u.index > peak_ts - pd.Timedelta(hours=24)) & (u.index <= peak_ts)].sort_index()
    out = {}
    for sig in signals:
        c = f"{sig}__z"
        if c not in u:
            out[sig] = None
            continue
        z = u[c].astype(float)
        start = None
        for ts, v in zip(z.index[::-1], z.to_numpy()[::-1]):
            if not (np.isfinite(v) and v > ONSET_Z):
                break
            start = ts
        out[sig] = _iso(start) if start is not None else None
    return out


def _explain(alert: dict, sc: dict, ctx: dict | None, prior: dict, cell: str,
             peak_ts: pd.Timestamp, cfg: dict) -> None:
    """Fill drivers / drivers_prior / shap (+ severity drivers) on ``alert`` in place.
    SHAP wins; disagreement with the fusion weights is logged, never overridden."""
    ecfg = explain_config(cfg)
    weights = {s["name"]: float(s["weight"]) for s in sc["signals"]}
    alert["drivers_prior"] = prior
    alert["drivers_method"] = "fusion_weights"
    alert["drivers"] = prior
    alert["drivers_severity"] = None
    alert["shap"] = None
    alert["shap_disagreements"] = []
    if not ctx:
        return
    try:
        x = ctx["X"].loc[[(cell, peak_ts)]]
    except KeyError:
        log.warning("%s: no surrogate features for %s at %s; fusion weights used",
                    alert["id"], cell, peak_ts)
        return
    for task in ("regression", "classification"):
        fitted = ctx.get(task)
        if fitted is None:
            continue
        model, bg, names = fitted
        xa = x.reindex(columns=names).astype(float)
        xa.index = [alert["id"]]
        try:
            t0 = time.perf_counter()
            contrib, feats, base = per_alert_contributions(
                alert["id"], model, bg, X_alerts=xa, explainer=ctx["explainers"].get(task),
                return_raw=True)
            ms = (time.perf_counter() - t0) * 1000
        except Exception as e:  # noqa: BLE001 - explanation must never block an alert
            log.warning("%s: SHAP (%s) failed: %s", alert["id"], task, e)
            continue
        pct = {k: round(100 * v["pct_of_total"], 1) for k, v in contrib.items()}
        payload = {
            "task": task,
            "output": "fused score" if task == "regression" else "log-odds of alert firing",
            "base_value": round(float(base), 6),
            "prediction": round(float(base + feats.sum()), 6),
            "signals": contrib,
            "features": {k: round(float(v), 6) for k, v in feats.items()},
            "x": {k: _num(v, 6) for k, v in xa.iloc[0].items()},
            "model": {k: v for k, v in getattr(model, "sentinel_meta_", {}).items()
                      if k in ("model_fitted_at", "n_train", "r2_holdout", "auc_holdout",
                               "best_iteration", "reused")},
            "background_rows": int(getattr(ctx["explainers"].get(task), "sentinel_background_rows_",
                                           len(bg))),
            "ms": round(ms, 2),
        }
        if task == "regression":
            alert["drivers"] = pct
            alert["drivers_method"] = "shap"
            alert["shap"] = payload
            for k, v in contrib.items():
                if (weights.get(k, 0) > 0 and v["direction"] == "lowers_risk"
                        and 100 * v["pct_of_total"] >= float(ecfg["disagreement_min_pct"])):
                    log.warning("%s %s @ %s: SHAP says %s LOWERS risk (%.0f%% of signal) although "
                                "its fusion weight %.2f says it raises it; keeping SHAP",
                                sc["id"], cell, _iso(peak_ts), k, 100 * v["pct_of_total"],
                                weights[k])
                    alert["shap_disagreements"].append(
                        {"signal": k, "fusion_weight": weights[k], "shap": v["shap"],
                         "pct_of_total": v["pct_of_total"]})
        else:
            alert["drivers_severity"] = pct
            alert["shap_severity"] = payload


SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}


def alert_id(scenario_id: str, cell: str, window_start) -> str:
    return str(uuid.uuid5(ALERT_NS, f"{scenario_id}|{cell}|{_iso(window_start)}"))


def alert_groups(fused: pd.DataFrame, as_of: pd.Timestamp, hours: int):
    """THE alerting rule, shared by build_alerts and the backtest replay.

    Yields (scenario_id, cell, window_start, expires_at, group_rows, peak_row, severity)
    for every scenario + hex with a severity in the previous or current ``hours``-hour
    UTC window that has not expired at ``as_of``."""
    f = fused[fused["severity"].notna() & (fused["ts_utc"] <= as_of)]
    for (w0, w1) in _window_bounds(as_of, hours):
        expires = w1 + pd.Timedelta(hours=hours)
        if expires <= as_of:
            continue
        win = f[(f["ts_utc"] >= w0) & (f["ts_utc"] < w1)]
        for (sid, cell), g in win.groupby(["scenario_id", "unit_h3"]):
            peak = g.loc[g["mean"].astype(float).idxmax()]
            sev = max(g["severity"], key=lambda s: SEVERITY_RANK[s])
            yield sid, cell, w0, expires, g, peak, sev


def newest_per_key(alerts: list[dict]) -> list[dict]:
    """A previous-window alert stays active until it expires, unless the current window
    already has its own alert for that scenario + hex (the newer one supersedes it)."""
    newest: dict = {}
    for a in alerts:
        k = (a["scenario_id"], a["h3_cell"])
        if k not in newest or a["window_start"] > newest[k]["window_start"]:
            newest[k] = a
    return list(newest.values())


def build_alerts(fused: pd.DataFrame, frame: pd.DataFrame, units: pd.DataFrame, cfg: dict,
                 as_of: pd.Timestamp, surrogates: dict | None = None) -> list[dict]:
    hours = int(cfg.get("fusion", {}).get("dedupe_window_hours", 6))
    scen = {s["id"]: s for s in cfg["scenarios"]}
    sig_meta = cfg["signals"]
    unit_by_h3 = units.set_index("h3")
    fr = frame.set_index(["unit_h3", "ts_utc"])
    rank = SEVERITY_RANK
    alerts = []
    for sid, cell, w0, expires, g, peak, sev in alert_groups(fused, as_of, hours):
        sc = scen[sid]
        weights = {s["name"]: float(s["weight"]) for s in sc["signals"]}
        row = fr.loc[(cell, peak["ts_utc"])]
        scores = {k: row.get(f"{k}__score", np.nan) for k in weights}
        details = {}
        u = unit_by_h3.loc[cell]
        for k, w in weights.items():
            pillar = sig_meta[k]["pillar"]
            details[k] = {
                "pillar": pillar, "weight": w, "unit": sig_meta[k].get("unit"),
                "label": sig_meta[k].get("label"),
                "value": _num(row.get(f"{k}__value")), "baseline": _num(row.get(f"{k}__baseline")),
                "z": _num(row.get(f"{k}__z")), "score": _num(row.get(f"{k}__score")),
                "suppressed": bool(row.get(f"{k}__suppressed", True)) if pd.notna(row.get(f"{k}__suppressed")) else True,
                "source": {"env": f"station {u['env_station']} ({u['env_distance_km']} km)",
                           "occ": "site roster (synthetic)",
                           "clin": f"{u['clinical_city']} catchment"}[pillar],
            }
        owner = sc["action_owner"] if sev != "warning" else sc.get("escalation_owner", sc["action_owner"])
        prior = contributions(scores, weights) or {
            k: round(100 * w / sum(weights.values()), 1) for k, w in weights.items()}
        alert = {
            "id": alert_id(sid, cell, w0),
            "scenario_id": sid,
            "scenario_label": sc["label"],
            "severity": sev,
            "h3_cell": cell,
            "sites_affected": list(u["site_ids"]),
            "site_names": list(u["site_names"]),
            "emirates": list(u["emirates"]),
            "clinical_catchment": u["clinical_city"],
            "score": round(float(peak["mean"]), 4),
            "credible_interval": [round(float(peak["ci_low"]), 4), round(float(peak["ci_high"]), 4)],
            "coverage": round(float(peak["coverage"]), 3),
            "drivers": prior,
            "driver_details": details,
            "created_at": _iso(g["ts_utc"].min()),
            "peak_at": _iso(peak["ts_utc"]),
            "window_start": _iso(w0),
            "expires_at": _iso(expires),
            "as_of": _iso(as_of),
            "action_owner": owner,
            "recommended_actions": list(sc["recommended_actions"][sev]),
            "hex_boundary": hex_boundary_geojson(cell),
        }
        _explain(alert, sc, (surrogates or {}).get(sid), prior, cell, peak["ts_utc"], cfg)
        alert["driver_onsets"] = _onsets(fr, cell, peak["ts_utc"], list(weights))
        alert["explanation_template"] = narrate_alert_template(alert)
        alerts.append(alert)
    # The groupby guarantees one alert per scenario per hex per window.
    alerts = sorted(newest_per_key(alerts),
                    key=lambda a: (-rank[a["severity"]], -a["score"], a["window_start"]))
    return alerts


def _num(x, nd: int = 4):
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if not np.isfinite(f) else round(f, nd)


ALERT_JSON_FIELDS = ("drivers", "drivers_prior", "drivers_severity", "driver_details",
                     "driver_onsets", "shap", "shap_severity", "shap_disagreements",
                     "hex_boundary", "action", "narration")


def alerts_frame(alerts: list[dict]) -> pd.DataFrame:
    cols = ["id", "scenario_id", "scenario_label", "severity", "h3_cell", "sites_affected",
            "site_names", "emirates", "clinical_catchment", "score", "credible_interval",
            "coverage", "drivers", "drivers_prior", "drivers_method", "drivers_severity",
            "driver_details", "driver_onsets", "shap", "shap_severity", "shap_disagreements",
            "explanation_template", "explanation_en", "explanation_ar", "explanation_source",
            "explanation_ar_source", "action", "narration", "created_at", "peak_at", "window_start",
            "expires_at", "as_of", "action_owner", "recommended_actions", "hex_boundary"]
    rows = [{**a, **{k: json.dumps(a.get(k)) for k in ALERT_JSON_FIELDS}} for a in alerts]
    df = pd.DataFrame(rows, columns=cols)
    for c in ("created_at", "peak_at", "window_start", "expires_at", "as_of"):
        df[c] = pd.to_datetime(df[c], utc=True)
    return df


def _write(df: pd.DataFrame, path: Path, meta: dict) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq
    path.parent.mkdir(parents=True, exist_ok=True)
    t = pa.Table.from_pandas(df, preserve_index=False)
    t = t.replace_schema_metadata({**(t.schema.metadata or {}),
                                   **{k.encode(): str(v).encode() for k, v in meta.items()}})
    tmp = path.with_suffix(".parquet.tmp")
    pq.write_table(t, tmp, compression="zstd")
    os.replace(tmp, path)


def generate_alerts(as_of_time, scenarios_yaml=SCENARIOS_YAML, *, mode: str | None = None,
                    processed_dir: Path = PROCESSED, out_dir: Path | None = ALERTS_DIR,
                    cache_dir: Path | None = CACHE_DIR, use_cache: bool = True,
                    explain_dir: Path | None = EXPLAIN_DIR,
                    panel_kwargs: dict | None = None, llm: bool = True,
                    narration_cache: NarrationCache | None = None) -> list[dict]:
    """Run the full pipeline as of ``as_of_time`` and return the active alerts.

    ``llm=False`` (or no GROQ_API_KEY) narrates every alert from templates. The narration
    cache defaults to <explain_dir>/narration_cache.sqlite (in memory if explain_dir is None)."""
    cfg = load_scenarios(scenarios_yaml)
    mode = mode or cfg.get("fusion", {}).get("mode", "map")
    as_of = pd.Timestamp(as_of_time)
    as_of = as_of.tz_localize("UTC") if as_of.tzinfo is None else as_of.tz_convert("UTC")
    timings = {}
    t0 = time.time()
    panel, units = build_signal_panel(as_of, cfg, processed_dir=processed_dir, **(panel_kwargs or {}))
    timings["panel_s"] = round(time.time() - t0, 2)
    t1 = time.time()
    fitted = fit_all(cfg, panel, as_of, cache_dir=cache_dir, use_cache=use_cache)
    timings["fit_s"] = round(time.time() - t1, 2)
    scored = detect(panel, cfg, as_of, fitted=fitted)
    frame = unit_signal_frame(scored, units, cfg)
    explain_meta: dict = {}
    if frame.empty:
        alerts: list[dict] = []
        fused = pd.DataFrame()
    else:
        hours = int(cfg.get("fusion", {}).get("dedupe_window_hours", 6))
        recent = frame["ts_utc"] >= as_of.floor(f"{hours}h") - pd.Timedelta(hours=hours)
        t2 = time.time()
        fused = fuse_all(frame, cfg, mode=mode, rows_for_sampled=np.where(recent)[0])
        timings["fuse_s"] = round(time.time() - t2, 2)
        t3 = time.time()
        surrogates = fit_scenario_surrogates(
            frame, fused, cfg, as_of, cache_dir=explain_dir, use_cache=use_cache,
            data_key=",".join(sorted(units["h3"])))
        timings["surrogate_s"] = round(time.time() - t3, 2)
        t4 = time.time()
        alerts = build_alerts(fused, frame, units, cfg, as_of, surrogates=surrogates)
        timings["build_explain_s"] = round(time.time() - t4, 2)
        shap_ms = [a["shap"]["ms"] for a in alerts if a.get("shap")]
        timings["shap_ms_max"] = round(max(shap_ms), 1) if shap_ms else None
        explain_meta = {sid: {t: (dict(getattr(c[t][0], "sentinel_meta_", {})) if c.get(t) else None)
                              for t in ("regression", "classification")}
                        for sid, c in surrogates.items()}
    t5 = time.time()
    if narration_cache is None:
        narration_cache = NarrationCache(Path(explain_dir) / "narration_cache.sqlite"
                                         if explain_dir is not None else ":memory:")
    narration_stats = narrate_alerts(alerts, cfg, cache=narration_cache, enabled=llm)
    timings["narrate_s"] = round(time.time() - t5, 2)
    timings["total_s"] = round(time.time() - t0, 2)

    if out_dir is not None:
        meta = {"as_of": _iso(as_of), "mode": mode, "notice": NOTICE, "timings": json.dumps(timings),
                "models": json.dumps({k: {"kind": v.baseline.kind, "reused": v.reused,
                                          "trained_until": _iso(v.baseline.trained_until)}
                                      for k, v in fitted.items()})}
        out_dir = Path(out_dir)
        _write(alerts_frame(alerts), out_dir / "active.parquet", meta)
        lo = as_of - pd.Timedelta(hours=TRACE_HOURS)
        sig_tr = scored[scored["ts_utc"] > lo].copy()
        _write(sig_tr[["signal", "pillar", "h3", "ts_utc", "value", "baseline", "z", "score",
                       "suppressed"]].reset_index(drop=True), out_dir / "signal_traces.parquet", meta)
        if not fused.empty:
            ft = fused[fused["ts_utc"] > lo][["scenario_id", "unit_h3", "ts_utc", "mean", "ci_low",
                                              "ci_high", "coverage", "eligible", "severity"]]
            _write(ft.reset_index(drop=True), out_dir / "fused_traces.parquet", meta)
        units_out = units.copy()
        for c in ("site_ids", "site_names", "emirates", "sectors"):
            units_out[c] = units_out[c].map(json.dumps)
        _write(units_out, out_dir / "units.parquet", meta)
        (out_dir / "meta.json").write_text(json.dumps(
            {"as_of": _iso(as_of), "mode": mode, "n_alerts": len(alerts), "timings": timings,
             "surrogates": explain_meta, "narration": narration_stats, "notice": NOTICE},
            indent=2, default=str))
        try:
            from packages.pipelines.ops.notify import send_new_alerts
            routing = send_new_alerts(alerts, out_dir / "sends.jsonl")
        except Exception as e:  # noqa: BLE001 - routing must never fail an alert run
            log.warning("n8n routing failed: %s", e)
            routing = {"enabled": True, "error": str(e)[:200]}
        meta_p = out_dir / "meta.json"
        m = json.loads(meta_p.read_text())
        m["routing"] = routing
        meta_p.write_text(json.dumps(m, indent=2, default=str))
    log.info("as_of=%s alerts=%d timings=%s", _iso(as_of), len(alerts), timings)
    return alerts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--as-of", default=None, help="ISO-8601 time (default: now, UTC)")
    ap.add_argument("--mode", choices=["map", "sampled"], default=None)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--out-dir", type=Path, default=ALERTS_DIR)
    ap.add_argument("--no-llm", action="store_true", help="template narration only (no Groq calls)")
    args = ap.parse_args(argv)
    try:                                   # local runs: pick up GROQ_* from .env
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parents[3] / ".env", override=False)
    except ImportError:
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    # The only clock read in the fusion package: the CLI decides "now".
    as_of = pd.Timestamp(args.as_of) if args.as_of else pd.Timestamp(datetime.now(timezone.utc))
    alerts = generate_alerts(as_of, mode=args.mode, use_cache=not args.no_cache, out_dir=args.out_dir,
                             llm=not args.no_llm)
    print(f"\n{len(alerts)} active alert(s) as of {_iso(as_of)}")
    for a in alerts:
        drv = ", ".join(f"{k} {v:.0f}%" for k, v in sorted(a["drivers"].items(), key=lambda kv: -kv[1]))
        print(f"  [{a['severity']:8}] {a['scenario_id']:26} {', '.join(a['site_names'])[:44]:44} "
              f"score {a['score']:.2f} [{a['credible_interval'][0]:.2f}, {a['credible_interval'][1]:.2f}]  "
              f"{a['drivers_method']}: {drv}")
        print(f"             [{a['explanation_source']}] {a['explanation_en']}")
        print(f"             [{a['explanation_ar_source']}] {a['explanation_ar']}")
        act = a["action"]
        print(f"             action [{act['source']}] {act['owner_role']} / {act['time_window']} / "
              f"escalate {act['escalation_if_no_ack_in_minutes']} min: {act['specific_action']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
