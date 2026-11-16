"""Per-signal baselines: what each signal "should" be at each hour, and how far off it is.

fit_prophet_baseline() fits ONE Prophet model per signal on a pooled series. Each hex is
normalised by its own mean/std, then the hexes are averaged, so a hot inland hex and a
cooler coastal hex share one daily/weekly shape. The baseline for hex h is
mean_h + std_h * yhat(t), and the residual z-score is (value - baseline) / (std_h * sigma),
where sigma is the robust spread of the pooled model's residuals.

Data handling rules:
* missing hours/days are gap-filled with NaN and never imputed; Prophet skips NaN rows
* fewer than ``min_history_days`` of data -> rolling_z() fallback (logged warning)
* outliers: the pooled training target is winsorised at the 1st/99th percentiles

Caching: models are pickled to data/processed/baselines/{signal}.pkl together with their
training cutoff. A cached model is reused only if its cutoff is <= the caller's
as_of_time (never a model trained on the future), so replays and backtests stay honest.

No function here reads the wall clock: every time comes from the caller.
"""
from __future__ import annotations

import hashlib
import json
import logging
import pickle
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger("sentinel.fusion.baselines")

REPO_ROOT = Path(__file__).resolve().parents[3]
CACHE_DIR = REPO_ROOT / "data" / "processed" / "baselines"
MODEL_VERSION = 1


def _quiet_prophet() -> None:
    for name in ("cmdstanpy", "prophet", "prophet.plot"):
        logging.getLogger(name).setLevel(logging.ERROR)


def gap_fill(series_df: pd.DataFrame, freq: str = "1h") -> pd.DataFrame:
    """Complete hourly grid per hex between the first and last timestamp; gaps stay NaN."""
    parts = []
    for cell, g in series_df.groupby("h3", sort=True):
        s = g.set_index("ts_utc")["value"].sort_index()
        s = s[~s.index.duplicated(keep="last")]
        grid = pd.date_range(s.index.min(), s.index.max(), freq=freq)
        parts.append(pd.DataFrame({"h3": cell, "ts_utc": grid, "value": s.reindex(grid).to_numpy()}))
    if not parts:
        return pd.DataFrame(columns=["h3", "ts_utc", "value"])
    return pd.concat(parts, ignore_index=True)


def rolling_z(series: pd.Series, window_days: int = 30, min_periods: int | None = None,
              min_std: float | None = None) -> pd.Series:
    """Trailing-window z-score: (x_t - mean(x in [t-window, t))) / std(...).

    The current point is excluded from its own window. ``series`` must have a
    DatetimeIndex. NaNs stay NaN. ``min_std`` floors the window std (e.g. for count
    series that are constant for weeks); without it a zero std gives NaN.
    """
    s = series.astype(float).sort_index()
    mp = min_periods if min_periods is not None else 24
    roll = s.rolling(f"{window_days}D", min_periods=mp, closed="left")
    mu, sd = roll.mean(), roll.std()
    sd = sd.clip(lower=min_std) if min_std else sd.where(sd > 1e-9)
    return (s - mu) / sd


def _robust_sigma(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if x.size < 3:
        return 1.0
    mad = np.median(np.abs(x - np.median(x))) * 1.4826
    sd = float(np.std(x))
    s = mad if mad > 1e-9 else sd
    return float(max(s, 1e-6))


@dataclass
class SignalBaseline:
    signal: str
    kind: str                                   # "prophet" | "rolling_z"
    trained_until: pd.Timestamp
    hex_stats: dict[str, tuple[float, float]]   # h3 -> (mean, std)
    sigma: float = 1.0
    prophet_json: str | None = None
    window_days: int = 30
    min_std: float = 1e-6
    config_hash: str = ""
    n_train: int = 0
    version: int = MODEL_VERSION
    notes: list[str] = field(default_factory=list)
    _model: object = field(default=None, repr=False, compare=False)

    # -- pickling: never pickle the live Prophet object (stan backend), only its JSON
    def __getstate__(self):
        d = dict(self.__dict__)
        d["_model"] = None
        return d

    def _prophet(self):
        if self._model is None and self.prophet_json is not None:
            from prophet.serialize import model_from_json
            _quiet_prophet()
            self._model = model_from_json(self.prophet_json)
        return self._model

    def residuals(self, series_df: pd.DataFrame) -> pd.DataFrame:
        """series_df (h3, ts_utc, value) -> + baseline, resid, z. Hexes unseen in
        training are normalised with their own stats from series_df."""
        df = gap_fill(series_df)
        if df.empty:
            return df.assign(baseline=np.nan, resid=np.nan, z=np.nan)
        stats = dict(self.hex_stats)
        for cell, g in df.groupby("h3"):
            if cell not in stats:
                v = g["value"].to_numpy(float)
                stats[cell] = (float(np.nanmean(v)) if np.isfinite(v).any() else 0.0,
                               float(np.nanstd(v)) if np.isfinite(v).any() else 1.0)
        mean = df["h3"].map(lambda c: stats[c][0]).astype(float)
        std = df["h3"].map(lambda c: stats[c][1]).astype(float)

        if self.kind == "prophet":
            m = self._prophet()
            ts = pd.DatetimeIndex(df["ts_utc"].unique()).sort_values()
            fc = m.predict(pd.DataFrame({"ds": ts.tz_convert("UTC").tz_localize(None)}))
            yhat = pd.Series(fc["yhat"].to_numpy(), index=ts)
            df["baseline"] = mean + std * df["ts_utc"].map(yhat).astype(float)
            df["resid"] = df["value"] - df["baseline"]
            df["z"] = df["resid"] / (std * self.sigma)
        else:
            parts = []
            for cell, g in df.groupby("h3", sort=False):
                s = g.set_index("ts_utc")["value"]
                roll = s.rolling(f"{self.window_days}D", min_periods=24, closed="left")
                parts.append(pd.DataFrame({"baseline": roll.mean().to_numpy(),
                                           "z": rolling_z(s, self.window_days,
                                                          min_std=self.min_std).to_numpy()},
                                          index=g.index))
            r = pd.concat(parts).reindex(df.index)
            df["baseline"] = r["baseline"]
            df["resid"] = df["value"] - df["baseline"]
            df["z"] = r["z"]
        return df


def baseline_config_hash(signal: str, seasonality, min_history_days: int, min_std: float,
                         rolling_window_days: int, winsor=(1.0, 99.0)) -> str:
    """Identity of a baseline's configuration; a cached model is reused only on a match."""
    kw = dict(signal=signal, seasonality=sorted(seasonality), min_history_days=int(min_history_days),
              min_std=float(min_std), window=int(rolling_window_days),
              winsor=[float(w) for w in winsor], version=MODEL_VERSION)
    return hashlib.sha1(json.dumps(kw, sort_keys=True).encode()).hexdigest()[:12]


def fit_prophet_baseline(series_df: pd.DataFrame, signal_name: str,
                         seasonality=frozenset({"daily", "weekly"}),
                         min_history_days: int = 30, *,
                         as_of_time=None, min_std: float = 1e-6,
                         rolling_window_days: int = 30,
                         winsor: tuple[float, float] = (1.0, 99.0),
                         cache_dir: Path | None = CACHE_DIR):
    """Fit a pooled Prophet baseline for one signal.

    series_df: long frame with columns h3, ts_utc (UTC), value. Only rows with
    ts_utc <= as_of_time are used for training (all rows if as_of_time is None).

    Returns (model, residuals_df), where residuals_df has h3, ts_utc, value, baseline,
    resid, z for every row of ``series_df`` up to as_of_time. With less than
    ``min_history_days`` of history the model is a rolling_z fallback (warning logged).
    The fitted model is written to ``cache_dir/{signal_name}.pkl`` unless cache_dir is None.
    """
    seasonality = set(seasonality)
    df = series_df[["h3", "ts_utc", "value"]].copy()
    df["ts_utc"] = pd.to_datetime(df["ts_utc"], utc=True)
    if as_of_time is not None:
        df = df[df["ts_utc"] <= pd.Timestamp(as_of_time).tz_convert("UTC")]
    df = gap_fill(df)
    finite = df[np.isfinite(df["value"].astype(float))]
    if finite.empty:
        raise ValueError(f"{signal_name}: no finite values to fit")
    trained_until = finite["ts_utc"].max()
    history_days = (trained_until - finite["ts_utc"].min()).total_seconds() / 86400

    stats = {}
    for cell, g in finite.groupby("h3"):
        v = g["value"].to_numpy(float)
        stats[cell] = (float(v.mean()), float(max(v.std(), min_std)))
    chash = baseline_config_hash(signal_name, seasonality, min_history_days, min_std,
                                 rolling_window_days, winsor)

    if history_days < min_history_days:
        log.warning("%s: only %.1f days of history (< %d); using rolling_z fallback",
                    signal_name, history_days, min_history_days)
        model = SignalBaseline(signal_name, "rolling_z", trained_until, stats,
                               window_days=rolling_window_days, min_std=min_std, config_hash=chash,
                               n_train=len(finite),
                               notes=[f"rolling_z fallback: {history_days:.1f} days history"])
    else:
        _quiet_prophet()
        from prophet import Prophet
        norm = df.copy()
        norm["y"] = [(v - stats[c][0]) / stats[c][1] if c in stats else np.nan
                     for c, v in zip(norm["h3"], norm["value"].astype(float))]
        pooled = norm.groupby("ts_utc")["y"].mean()        # NaN if every hex is NaN (gap)
        lo, hi = np.nanpercentile(pooled, winsor)
        pooled = pooled.clip(lo, hi)                       # winsorise outlier hours
        train = pd.DataFrame({"ds": pooled.index.tz_convert("UTC").tz_localize(None),
                              "y": pooled.to_numpy()})
        m = Prophet(daily_seasonality="daily" in seasonality,
                    weekly_seasonality="weekly" in seasonality,
                    yearly_seasonality=False, changepoint_prior_scale=0.05,
                    uncertainty_samples=0)
        m.fit(train.dropna())
        fitted = m.predict(train[["ds"]])["yhat"].to_numpy()
        sigma = _robust_sigma(train["y"].to_numpy() - fitted)
        from prophet.serialize import model_to_json
        model = SignalBaseline(signal_name, "prophet", trained_until, stats, sigma=sigma,
                               prophet_json=model_to_json(m), window_days=rolling_window_days,
                               min_std=min_std, config_hash=chash, n_train=int(train["y"].notna().sum()))
        model._model = m

    if cache_dir is not None:
        save_model(model, cache_dir)
    return model, model.residuals(df)


def save_model(model: SignalBaseline, cache_dir: Path = CACHE_DIR) -> Path:
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    p = cache_dir / f"{model.signal}.pkl"
    tmp = p.with_suffix(".pkl.tmp")
    with open(tmp, "wb") as f:
        pickle.dump(model, f)
    tmp.replace(p)
    return p


def load_cached(signal: str, cache_dir: Path = CACHE_DIR):
    p = Path(cache_dir) / f"{signal}.pkl"
    if not p.is_file():
        return None
    try:
        with open(p, "rb") as f:
            return pickle.load(f)
    except Exception as e:  # noqa: BLE001 — corrupt / incompatible cache = refit
        log.warning("ignoring unreadable cache %s: %s", p, e)
        return None
