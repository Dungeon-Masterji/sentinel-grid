"""Per-signal anomaly detection: PyOD ensemble on baseline residuals.

For each unique signal (shared across scenarios, fitted once):

  1. baseline  (baselines.fit_prophet_baseline): value -> residual z-score
  2. detectors IForest + ECOD + LOF fitted on the pooled z-scores of every hex up to
     as_of_time. Each detector's raw score is standardised against its own training
     scores; the ensemble takes the MEDIAN, then squashes it to [0, 1] with
     sigmoid(slope * (median - center)).
  3. gating    the scenarios care about upper-tail excursions only, so hours whose
     residual is at or below baseline score 0. So do hours below the signal's physical
     floor (e.g. apparent temperature < 38 °C).

Replay: detect() and fit_all() take ``as_of_time`` from the caller. Nothing here reads
the clock. Training uses only rows with ts_utc <= as_of_time, and cached models newer
than as_of_time are ignored.
"""
from __future__ import annotations

import logging
import pickle
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from packages.pipelines.fusion.baselines import (CACHE_DIR, SignalBaseline, baseline_config_hash,
                                                 fit_prophet_baseline, load_cached)
from packages.pipelines.fusion.config import load_scenarios, unique_signals

log = logging.getLogger("sentinel.fusion.detect")
DETECTOR_VERSION = 1


@dataclass
class DetectorEnsemble:
    """IForest + ECOD + LOF on a 1-D residual feature, median-combined."""
    center: float = 2.0
    slope: float = 1.5
    contamination: float = 0.02
    max_train_rows: int = 8000
    lof_neighbors: int = 50
    random_state: int = 42
    detectors: dict | None = None
    norms: dict | None = None
    trained_until: pd.Timestamp | None = None
    n_train: int = 0
    version: int = DETECTOR_VERSION

    def fit(self, x: np.ndarray) -> "DetectorEnsemble":
        from pyod.models.ecod import ECOD
        from pyod.models.iforest import IForest
        from pyod.models.lof import LOF
        x = np.asarray(x, dtype=float)
        x = x[np.isfinite(x)]
        if x.size < 20:
            raise ValueError("need at least 20 finite points to fit detectors")
        rng = np.random.default_rng(self.random_state)
        if x.size > self.max_train_rows:
            x = rng.choice(x, size=self.max_train_rows, replace=False)
        X = x.reshape(-1, 1)
        self.detectors = {
            "iforest": IForest(contamination=self.contamination, random_state=self.random_state,
                               n_estimators=100),
            "ecod": ECOD(contamination=self.contamination),
            "lof": LOF(contamination=self.contamination,
                       n_neighbors=min(self.lof_neighbors, max(5, X.shape[0] // 10))),
        }
        self.norms = {}
        for name, det in self.detectors.items():
            det.fit(X)
            s = np.asarray(det.decision_scores_, dtype=float)
            med = float(np.median(s))
            mad = float(np.median(np.abs(s - med)) * 1.4826) or float(np.std(s)) or 1.0
            self.norms[name] = (med, mad)
        self.n_train = int(X.shape[0])
        return self

    def raw_scores(self, x: np.ndarray) -> np.ndarray:
        """Median of per-detector standardised scores; NaN where x is NaN."""
        x = np.asarray(x, dtype=float)
        out = np.full(x.shape, np.nan)
        ok = np.isfinite(x)
        if not ok.any():
            return out
        X = x[ok].reshape(-1, 1)
        stack = []
        for name, det in self.detectors.items():
            med, mad = self.norms[name]
            stack.append((np.asarray(det.decision_function(X), dtype=float) - med) / mad)
        out[ok] = np.median(np.vstack(stack), axis=0)
        return out

    def score(self, x: np.ndarray) -> np.ndarray:
        r = self.raw_scores(x)
        with np.errstate(over="ignore"):
            return 1.0 / (1.0 + np.exp(-self.slope * (r - self.center)))


def score_signal(series, detector_ensemble: DetectorEnsemble, *, direction: str = "up",
                 values=None, floor: float | None = None) -> np.ndarray:
    """Per-timestep anomaly scores in [0, 1] for a residual z-score series.

    direction="up": below-baseline residuals score 0. ``floor``: timesteps whose raw
    ``values`` are below it score 0 (physical hazard gate). NaN in -> NaN out.
    """
    z = np.asarray(series, dtype=float)
    s = detector_ensemble.score(z)
    if direction == "up":
        s = np.where(np.isfinite(z) & (z <= 0), 0.0, s)
    if floor is not None and values is not None:
        v = np.asarray(values, dtype=float)
        s = np.where(np.isfinite(v) & (v < floor), 0.0, s)
    return s


@dataclass
class FittedSignal:
    name: str
    baseline: SignalBaseline
    detector: DetectorEnsemble
    reused: bool = False


def _detector_path(cache_dir: Path, signal: str) -> Path:
    return Path(cache_dir) / f"{signal}.detector.pkl"


def _usable(cached, as_of: pd.Timestamp, refit_after_h: float, chash: str | None = None) -> bool:
    if cached is None or getattr(cached, "trained_until", None) is None:
        return False
    tu = pd.Timestamp(cached.trained_until)
    if tu > as_of:                                    # trained on the future: never reuse
        return False
    if (as_of - tu) > pd.Timedelta(hours=refit_after_h):
        return False
    return chash is None or getattr(cached, "config_hash", None) == chash


def fit_all(scenarios_yaml, panel: pd.DataFrame, as_of_time, *,
            cache_dir: Path | None = CACHE_DIR, use_cache: bool = True) -> dict[str, FittedSignal]:
    """One baseline + one detector per unique signal across all scenarios.

    panel: long frame (signal, h3, ts_utc, value, ...), the output of
    packages/pipelines/fusion/signals.build_signal_panel.
    """
    cfg = load_scenarios(scenarios_yaml)
    as_of = pd.Timestamp(as_of_time).tz_convert("UTC")
    bcfg, dcfg = cfg.get("baseline", {}), cfg.get("detector", {})
    refit_h = float(bcfg.get("refit_after_hours", 24))
    out: dict[str, FittedSignal] = {}
    for name in unique_signals(cfg):
        t0 = time.time()
        meta = cfg["signals"][name]
        sdf = panel[(panel["signal"] == name) & (panel["ts_utc"] <= as_of)]
        if sdf["value"].notna().sum() < 20:
            log.warning("%s: not enough data to fit; skipped", name)
            continue
        cached_b = load_cached(name, cache_dir) if (use_cache and cache_dir) else None
        cached_d = None
        if use_cache and cache_dir and _detector_path(cache_dir, name).is_file():
            try:
                with open(_detector_path(cache_dir, name), "rb") as f:
                    cached_d = pickle.load(f)
            except Exception:  # noqa: BLE001
                cached_d = None
        seasonality = set(bcfg.get("seasonality", ["daily", "weekly"]))
        min_hist = int(bcfg.get("min_history_days", 30))
        min_std = float(meta.get("min_std", 1e-6))
        window = int(bcfg.get("rolling_window_days", 30))
        want = baseline_config_hash(name, seasonality, min_hist, min_std, window)
        baseline_ok = _usable(cached_b, as_of, refit_h, want)
        detector_ok = (baseline_ok and cached_d is not None
                       and cached_d.trained_until == cached_b.trained_until
                       and cached_d.version == DETECTOR_VERSION)
        if baseline_ok and detector_ok:
            out[name] = FittedSignal(name, cached_b, cached_d, reused=True)
            log.info("%s: reused cached models (trained until %s)", name, cached_b.trained_until)
            continue

        baseline, resid = fit_prophet_baseline(
            sdf[["h3", "ts_utc", "value"]], name, seasonality=seasonality,
            min_history_days=min_hist, as_of_time=as_of, min_std=min_std,
            rolling_window_days=window, cache_dir=cache_dir)
        det = DetectorEnsemble(center=float(dcfg.get("center", 2.0)),
                               slope=float(dcfg.get("slope", 1.5)),
                               contamination=float(dcfg.get("contamination", 0.02)),
                               max_train_rows=int(dcfg.get("max_train_rows", 8000)),
                               lof_neighbors=int(dcfg.get("lof_neighbors", 50)),
                               random_state=int(dcfg.get("random_state", 42)))
        try:
            det.fit(resid["z"].to_numpy())
        except ValueError as e:
            log.warning("%s: detectors not fitted (%s); signal treated as missing", name, e)
            continue
        det.trained_until = baseline.trained_until
        if cache_dir is not None:
            p = _detector_path(cache_dir, name)
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p.with_suffix(".tmp"), "wb") as f:
                pickle.dump(det, f)
            p.with_suffix(".tmp").replace(p)
        out[name] = FittedSignal(name, baseline, det)
        log.info("%s: fitted %s baseline + detectors in %.1fs", name, baseline.kind, time.time() - t0)
    return out


def detect(panel: pd.DataFrame, scenarios_yaml, as_of_time, *,
           fitted: dict[str, FittedSignal] | None = None,
           cache_dir: Path | None = CACHE_DIR, use_cache: bool = True) -> pd.DataFrame:
    """Score every signal at every (hex, hour) up to ``as_of_time``.

    Returns long frame: signal, pillar, h3, ts_utc, value, baseline, z, score, plus
    n_records/suppressed carried from the panel.
    """
    cfg = load_scenarios(scenarios_yaml)
    as_of = pd.Timestamp(as_of_time).tz_convert("UTC")
    panel = panel[panel["ts_utc"] <= as_of]
    fitted = fitted or fit_all(cfg, panel, as_of, cache_dir=cache_dir, use_cache=use_cache)
    parts = []
    for name, fs in fitted.items():
        meta = cfg["signals"][name]
        sdf = panel[panel["signal"] == name]
        if sdf.empty:
            continue
        r = fs.baseline.residuals(sdf[["h3", "ts_utc", "value"]])
        r = r[r["ts_utc"] <= as_of]
        r["score"] = score_signal(r["z"].to_numpy(), fs.detector, direction="up",
                                  values=r["value"].to_numpy(), floor=meta.get("floor"))
        extra = sdf[["h3", "ts_utc", "n_records", "suppressed"]].drop_duplicates(["h3", "ts_utc"])
        r = r.merge(extra, on=["h3", "ts_utc"], how="left")
        r["suppressed"] = r["suppressed"].astype("boolean").fillna(True).astype(bool)
        r["signal"] = name
        r["pillar"] = meta["pillar"]
        parts.append(r)
    cols = ["signal", "pillar", "h3", "ts_utc", "value", "baseline", "z", "score",
            "n_records", "suppressed"]
    if not parts:
        return pd.DataFrame(columns=cols)
    return pd.concat(parts, ignore_index=True)[cols]
