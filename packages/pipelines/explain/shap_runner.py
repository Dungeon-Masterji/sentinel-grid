"""Per-alert SHAP explanations from a LightGBM surrogate of the fusion detector.

The fusion score (combine.py) is a closed-form opinion pool, so its "weights" say how
much each signal COULD matter, not how much it DID for this hex at this hour. To get
data-grounded drivers we fit, per scenario, a gradient-boosted surrogate that maps the
detector's own inputs to its output, and explain the alert row with TreeSHAP:

    X  per (unit hex, hour), for every signal in the scenario:
         <signal>__value    raw reading (what the physical floor gates on)
         <signal>__z        baseline residual z-score (what the PyOD detectors see)
         <signal>__score    detector anomaly score (what the fusion pools)
         <signal>__z_lag1   z one hour earlier   (trend)
         <signal>__z_lag3   z three hours earlier
    y  task="regression"      fused posterior mean (the alert score)
       task="classification"  1 if the hex-hour reached watch or above (severity fired)

    model  lightgbm.LGBMRegressor / LGBMClassifier, max_depth=5, n_estimators=100,
           early stopping on a 20% hold-out.
    SHAP   shap.TreeExplainer(feature_perturbation="interventional") against a
           background sample of training rows, so attributions are "this hour versus
           a typical hour in the last 90 days at these hexes".

Lag and companion columns are collapsed into the scenario's named signal before
display (pm25__value + pm25__z + pm25__z_lag1 + ... -> "pm25"): SHAP values are
additive, so the sum is that signal's exact net effect.

Caching: data/processed/explain/surrogate_{scenario_id}_{task}.pkl. A cached surrogate
is reused while the window it was fitted on (model_fitted_at = the as_of time of the fit)
is no more than explain.retrain_after_hours (12) behind the current as_of, never if it
was fitted on a later window, and only if the scenario config, feature set and fusion
units are unchanged.

Nothing here reads the clock: as_of comes from the caller.
"""
from __future__ import annotations

import hashlib
import json
import logging
import pickle
import time
from pathlib import Path

import numpy as np
import pandas as pd

from packages.pipelines.fusion.config import load_scenarios

log = logging.getLogger("sentinel.explain.shap")

REPO_ROOT = Path(__file__).resolve().parents[3]
EXPLAIN_DIR = REPO_ROOT / "data" / "processed" / "explain"
SURROGATE_VERSION = 1
SEP = "__"
FEATURE_FIELDS = ("value", "z", "score")
LAGS = (1, 3)
TASKS = ("regression", "classification")
TARGET_COLUMN = {"regression": "fused_score", "classification": "alert_fired"}
LGBM_PARAMS = dict(max_depth=5, num_leaves=31, n_estimators=100, learning_rate=0.1,
                   min_child_samples=20, subsample=1.0, colsample_bytree=1.0,
                   deterministic=True, force_col_wise=True, verbose=-1)
DEFAULTS = dict(retrain_after_hours=12, background_rows=1000, background_fallback_rows=500,
                shap_budget_ms=300, min_train_rows=200, min_class_rows=10,
                early_stopping_rounds=10, validation_fraction=0.2,
                disagreement_min_pct=5.0, random_state=42)


class SurrogateFitError(RuntimeError):
    """Not enough usable data to fit a surrogate (e.g. sparse hexes, no positives)."""


def explain_config(cfg: dict | None) -> dict:
    return {**DEFAULTS, **((cfg or {}).get("explain") or {})}


def signal_of(feature: str) -> str:
    """'pm25__z_lag3' -> 'pm25'."""
    return feature.split(SEP, 1)[0]


def _scenario(cfg: dict, scenario_id: str) -> dict:
    for sc in cfg["scenarios"]:
        if sc["id"] == scenario_id:
            return sc
    raise KeyError(f"unknown scenario {scenario_id!r}")


# ---------------------------------------------------------------------------
# features
# ---------------------------------------------------------------------------

def feature_names_for(scenario: dict) -> list[str]:
    out = []
    for s in scenario["signals"]:
        out += [f"{s['name']}{SEP}{f}" for f in FEATURE_FIELDS]
        out += [f"{s['name']}{SEP}z_lag{L}" for L in LAGS]
    return out


def build_features(frame: pd.DataFrame, scenario: dict,
                   fused: pd.DataFrame | None = None) -> pd.DataFrame:
    """Surrogate design matrix for one scenario, one row per (unit_h3, ts_utc).

    frame: fusion.alerts.unit_signal_frame output ('<signal>__<field>' columns).
    fused: fusion.alerts.fuse_all output; if given, adds the targets fused_score and
           alert_fired (rows where the fusion was undefined get NaN targets).
    Lags are time-based (ts - L hours at the same unit), so gaps never shift a lag.
    """
    f = frame.sort_values(["unit_h3", "ts_utc"]).reset_index(drop=True)
    out = pd.DataFrame({"unit_h3": f["unit_h3"].to_numpy(), "ts_utc": f["ts_utc"].to_numpy()})
    key = pd.MultiIndex.from_arrays([f["unit_h3"], f["ts_utc"]])
    for s in scenario["signals"]:
        name = s["name"]
        for fld in FEATURE_FIELDS:
            c = f"{name}{SEP}{fld}"
            out[c] = pd.to_numeric(f[c], errors="coerce").to_numpy(float) if c in f else np.nan
        z = pd.Series(out[f"{name}{SEP}z"].to_numpy(), index=key)
        z = z[~z.index.duplicated()]
        for L in LAGS:
            lagged = pd.MultiIndex.from_arrays([f["unit_h3"], f["ts_utc"] - pd.Timedelta(hours=L)])
            out[f"{name}{SEP}z_lag{L}"] = z.reindex(lagged).to_numpy()
    if fused is not None:
        fz = fused[fused["scenario_id"] == scenario["id"]][["unit_h3", "ts_utc", "mean", "severity"]]
        out = out.merge(fz, on=["unit_h3", "ts_utc"], how="left")
        out["fused_score"] = pd.to_numeric(out.pop("mean"), errors="coerce")
        sev = out.pop("severity")
        out["alert_fired"] = np.where(out["fused_score"].notna(), sev.notna().astype(float), np.nan)
    return out


# ---------------------------------------------------------------------------
# fitting + cache
# ---------------------------------------------------------------------------

def surrogate_path(cache_dir: Path, scenario_id: str, task: str) -> Path:
    return Path(cache_dir) / f"surrogate_{scenario_id}_{task}.pkl"


def _config_hash(scenario: dict, feature_names: list[str], task: str, data_key: str,
                 ecfg: dict) -> str:
    blob = json.dumps({"v": SURROGATE_VERSION, "task": task, "features": feature_names,
                       "signals": scenario["signals"], "thresholds": scenario["thresholds"],
                       "params": LGBM_PARAMS, "data": data_key,
                       "bg": [ecfg["background_rows"], ecfg["random_state"]]},
                      sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _usable(payload: dict | None, as_of: pd.Timestamp, chash: str, retrain_h: float) -> bool:
    if not payload or payload.get("config_hash") != chash:
        return False
    fitted = pd.Timestamp(payload["model_fitted_at"])
    if fitted > as_of:                       # fitted on a later window: never reuse in replay
        return False
    return (as_of - fitted) <= pd.Timedelta(hours=retrain_h)


def _load(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        with open(path, "rb") as fh:
            return pickle.load(fh)
    except Exception as e:  # noqa: BLE001 - a corrupt cache just means "refit"
        log.warning("ignoring unreadable surrogate cache %s (%s)", path.name, e)
        return None


def _save(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "wb") as fh:
        pickle.dump(payload, fh)
    tmp.replace(path)


def fit_surrogate(features_df: pd.DataFrame, scenario_id: str, *, task: str = "regression",
                  scenarios_yaml=None, as_of_time=None, cache_dir: Path | None = EXPLAIN_DIR,
                  use_cache: bool = True, data_key: str = ""):
    """Fit (or load from cache) the LightGBM surrogate for one scenario.

    features_df: build_features(frame, scenario, fused) output: feature columns plus
                 the targets fused_score (regression) and alert_fired (classification).
    Returns (model, X_train_sample, feature_names). X_train_sample is the SHAP
    background (<= explain.background_rows training rows). Cache metadata is on
    ``model.sentinel_meta_`` (model_fitted_at, reused, metric, n_train, ...).
    Raises SurrogateFitError when the data cannot support a fit.
    """
    import lightgbm as lgb

    if task not in TASKS:
        raise ValueError(f"task must be one of {TASKS}")
    cfg = load_scenarios(scenarios_yaml) if scenarios_yaml is not None else load_scenarios()
    ecfg = explain_config(cfg)
    sc = _scenario(cfg, scenario_id)
    names = feature_names_for(sc)
    as_of = (pd.Timestamp(as_of_time) if as_of_time is not None
             else pd.Timestamp(features_df["ts_utc"].max()))
    as_of = as_of.tz_localize("UTC") if as_of.tzinfo is None else as_of.tz_convert("UTC")
    chash = _config_hash(sc, names, task, data_key, ecfg)
    path = surrogate_path(cache_dir, scenario_id, task) if cache_dir is not None else None

    if use_cache and path is not None:
        cached = _load(path)
        if _usable(cached, as_of, chash, float(ecfg["retrain_after_hours"])):
            model = cached["model"]
            model.sentinel_meta_ = {**cached["meta"], "reused": True}
            log.info("%s/%s: reused surrogate fitted at %s", scenario_id, task,
                     cached["model_fitted_at"])
            return model, cached["background"], cached["feature_names"]

    t0 = time.time()
    df = features_df
    if "ts_utc" in df:
        df = df[pd.to_datetime(df["ts_utc"], utc=True) <= as_of]
    target = TARGET_COLUMN[task]
    if target not in df:
        raise SurrogateFitError(f"features_df has no {target!r} column")
    df = df[np.isfinite(pd.to_numeric(df[target], errors="coerce"))]
    X = df.reindex(columns=names).astype(float)
    y = df[target].astype(float).to_numpy()
    usable = X.notna().any(axis=1).to_numpy()
    X, y = X[usable], y[usable]
    if len(X) < int(ecfg["min_train_rows"]):
        raise SurrogateFitError(f"{scenario_id}/{task}: {len(X)} usable rows "
                                f"(< {ecfg['min_train_rows']})")
    if task == "classification":
        pos = int(y.sum())
        if pos < ecfg["min_class_rows"] or len(y) - pos < ecfg["min_class_rows"]:
            raise SurrogateFitError(f"{scenario_id}/classification: {pos} positive rows of "
                                    f"{len(y)}; need >= {ecfg['min_class_rows']} of each class")
    elif float(np.nanstd(y)) < 1e-9:
        raise SurrogateFitError(f"{scenario_id}/regression: constant target")

    rng = np.random.default_rng(int(ecfg["random_state"]))
    idx = rng.permutation(len(X))
    n_val = max(1, int(len(X) * float(ecfg["validation_fraction"])))
    if task == "classification":                       # keep positives in both splits
        pos_i, neg_i = idx[y[idx] == 1], idx[y[idx] == 0]
        kp, kn = max(1, int(len(pos_i) * ecfg["validation_fraction"])), \
            max(1, int(len(neg_i) * ecfg["validation_fraction"]))
        va, tr = np.r_[pos_i[:kp], neg_i[:kn]], np.r_[pos_i[kp:], neg_i[kn:]]
    else:
        va, tr = idx[:n_val], idx[n_val:]
    params = {**LGBM_PARAMS, "random_state": int(ecfg["random_state"])}
    model = (lgb.LGBMRegressor(**params) if task == "regression"
             else lgb.LGBMClassifier(**params))
    model.fit(X.iloc[tr], y[tr], eval_X=(X.iloc[va],), eval_y=(y[va],),
              callbacks=[lgb.early_stopping(int(ecfg["early_stopping_rounds"]), verbose=False)])

    if task == "regression":
        pred = model.predict(X.iloc[va])
        ss = float(np.sum((y[va] - y[va].mean()) ** 2)) or 1.0
        metric = {"r2_holdout": round(1 - float(np.sum((y[va] - pred) ** 2)) / ss, 4)}
    else:
        from sklearn.metrics import roc_auc_score
        p = model.predict_proba(X.iloc[va])[:, 1]
        metric = {"auc_holdout": round(float(roc_auc_score(y[va], p)), 4)
                  if 0 < y[va].sum() < len(va) else None}

    n_bg = min(int(ecfg["background_rows"]), len(X))
    background = X.iloc[np.sort(rng.choice(len(X), size=n_bg, replace=False))].reset_index(drop=True)
    meta = {"scenario_id": scenario_id, "task": task, "model_fitted_at": as_of.isoformat(),
            "n_train": int(len(tr)), "n_holdout": int(len(va)),
            "best_iteration": int(model.best_iteration_ or params["n_estimators"]),
            "fit_s": round(time.time() - t0, 3), **metric, "reused": False}
    model.sentinel_meta_ = meta
    if path is not None:
        _save(path, {"model": model, "background": background, "feature_names": names,
                     "model_fitted_at": as_of.isoformat(), "config_hash": chash, "meta": meta})
    log.info("%s/%s: fitted surrogate on %d rows in %.2fs %s", scenario_id, task, len(tr),
             meta["fit_s"], metric)
    return model, background, names


# ---------------------------------------------------------------------------
# SHAP
# ---------------------------------------------------------------------------

_EXPLAINERS: dict[tuple[int, int], object] = {}


def _positive_class(values, expected):
    """Normalise TreeExplainer output for binary classifiers to the positive class."""
    if isinstance(values, list):
        values = values[-1]
    values = np.asarray(values, dtype=float)
    if values.ndim == 3:
        values = values[..., -1]
    e = np.atleast_1d(np.asarray(expected, dtype=float))
    return values, float(e[-1])


def make_explainer(model, X_background: pd.DataFrame, *, budget_ms: float | None = None,
                   fallback_rows: int | None = None, random_state: int = 42):
    """Interventional TreeExplainer. If one row takes longer than ``budget_ms`` to
    explain, the background is downsampled to ``fallback_rows`` and rebuilt."""
    import shap

    budget_ms = DEFAULTS["shap_budget_ms"] if budget_ms is None else budget_ms
    fallback_rows = DEFAULTS["background_fallback_rows"] if fallback_rows is None else fallback_rows
    key = (id(model), id(X_background))
    if key in _EXPLAINERS:
        return _EXPLAINERS[key]
    bg = X_background
    exp = shap.TreeExplainer(model, data=bg, feature_perturbation="interventional")
    t0 = time.perf_counter()
    exp.shap_values(bg.iloc[:1])
    ms = (time.perf_counter() - t0) * 1000
    if ms > budget_ms and len(bg) > fallback_rows:
        log.info("SHAP %.0f ms/row > %.0f ms budget: downsampling background %d -> %d",
                 ms, budget_ms, len(bg), fallback_rows)
        bg = bg.sample(n=fallback_rows, random_state=random_state)
        exp = shap.TreeExplainer(model, data=bg, feature_perturbation="interventional")
    exp.sentinel_background_rows_ = len(bg)
    exp.sentinel_probe_ms_ = round(ms, 2)
    if len(_EXPLAINERS) > 64:
        _EXPLAINERS.clear()
    _EXPLAINERS[key] = exp
    return exp


def compute_shap_values(model, X_sample: pd.DataFrame, X_background: pd.DataFrame, *,
                        explainer=None) -> pd.DataFrame:
    """Per-row, per-feature SHAP contributions (model output units: score for the
    regressor, log-odds for the classifier). ``.attrs['base_value']`` holds the
    expected model output over the background, so for every row
    base_value + row.sum() == model output."""
    exp = explainer or make_explainer(model, X_background)
    Xs = X_sample.reindex(columns=list(X_background.columns)).astype(float)
    vals, base = _positive_class(exp.shap_values(Xs), exp.expected_value)
    out = pd.DataFrame(vals.reshape(len(Xs), -1), index=X_sample.index, columns=Xs.columns)
    out.attrs["base_value"] = base
    return out


def collapse_to_signals(shap_row: pd.Series) -> dict[str, float]:
    """Sum feature-level SHAP into the scenario's named signals (exact, by additivity)."""
    out: dict[str, float] = {}
    for feat, v in shap_row.items():
        out[signal_of(feat)] = out.get(signal_of(feat), 0.0) + float(v)
    return out


def per_alert_contributions(alert_id: str, model, X_background: pd.DataFrame, *,
                            X_alerts: pd.DataFrame, explainer=None,
                            return_raw: bool = False):
    """SHAP drivers for one alert.

    X_alerts: feature rows indexed by alert id (the alert's unit hex at its peak hour).
    Returns {signal: {"abs_shap", "shap", "pct_of_total", "direction"}} where pct_of_total
    is |net SHAP of the signal| / sum over signals (sums to 1) and direction is
    'raises_risk' (net SHAP > 0) or 'lowers_risk'. With return_raw=True also returns
    (feature-level SHAP Series, base_value).
    """
    row = X_alerts.loc[[alert_id]]
    sv = compute_shap_values(model, row, X_background, explainer=explainer)
    feats = sv.iloc[0]
    net = collapse_to_signals(feats)
    tot = sum(abs(v) for v in net.values())
    out = {}
    for sig, v in net.items():
        out[sig] = {"abs_shap": round(abs(v), 6), "shap": round(v, 6),
                    "pct_of_total": round(abs(v) / tot, 6) if tot > 0 else round(1 / len(net), 6),
                    "direction": "raises_risk" if v > 0 else "lowers_risk"}
    if return_raw:
        return out, feats, sv.attrs["base_value"]
    return out


def waterfall(feats: pd.Series, base_value: float, x_row: pd.Series | None = None, *,
              level: str = "signal") -> dict:
    """Waterfall steps (largest |SHAP| first) from base_value to the model output.

    Each step: feature, shap, baseline (running total before it), cumulative (after),
    and the feature's value. level='signal' collapses lag/companion columns."""
    if level == "signal":
        items = collapse_to_signals(feats)
        vals = {k: (None if x_row is None else _num(x_row.get(f"{k}{SEP}value"))) for k in items}
    else:
        items = {k: float(v) for k, v in feats.items()}
        vals = {k: (None if x_row is None else _num(x_row.get(k))) for k in items}
    run = float(base_value)
    steps = []
    for k, v in sorted(items.items(), key=lambda kv: -abs(kv[1])):
        steps.append({"feature": k, "signal": signal_of(k), "shap": round(v, 6),
                      "value": vals[k], "baseline": round(run, 6), "cumulative": round(run + v, 6)})
        run += v
    return {"level": level, "base_value": round(float(base_value), 6),
            "prediction": round(run, 6), "steps": steps}


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if not np.isfinite(v) else round(v, 4)


# ---------------------------------------------------------------------------
# orchestration for alerts.py
# ---------------------------------------------------------------------------

def fit_scenario_surrogates(frame: pd.DataFrame, fused: pd.DataFrame, cfg: dict, as_of, *,
                            cache_dir: Path | None = EXPLAIN_DIR, use_cache: bool = True,
                            data_key: str = "") -> dict[str, dict]:
    """{scenario_id: {"X": features indexed by (unit_h3, ts_utc), task: (model, bg, names)
    or None}} for every scenario. A failed fit is logged and left None (the caller
    falls back to the fusion weights)."""
    ecfg = explain_config(cfg)
    out = {}
    for sc in cfg["scenarios"]:
        feats = build_features(frame, sc, fused)
        entry = {"X": feats.set_index(["unit_h3", "ts_utc"]), "explainers": {}}
        for task in TASKS:
            try:
                model, bg, names = fit_surrogate(feats, sc["id"], task=task, scenarios_yaml=cfg,
                                                 as_of_time=as_of, cache_dir=cache_dir,
                                                 use_cache=use_cache, data_key=data_key)
                entry[task] = (model, bg, names)
                entry["explainers"][task] = make_explainer(
                    model, bg, budget_ms=float(ecfg["shap_budget_ms"]),
                    fallback_rows=int(ecfg["background_fallback_rows"]),
                    random_state=int(ecfg["random_state"]))
            except Exception as e:  # noqa: BLE001 - sparse hexes etc.: fall back, never crash
                lvl = logging.INFO if task == "classification" else logging.WARNING
                log.log(lvl, "%s/%s surrogate unavailable (%s)", sc["id"], task, e)
                entry[task] = None
        out[sc["id"]] = entry
    return out
