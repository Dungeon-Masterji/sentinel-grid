"""Synthetic workforce, shift log and daily site rollup for the occupational pillar.

EVERY WORKER HERE IS SYNTHETIC. Demographics are modelled on public aggregates
(configs/workforce_mix.yaml) and must not be used for individual employment
decisions (docs/ETHICS.md). Workers are identified only by opaque ids; no names
are generated.

Public entry points:

* generate_workforce(sites_yaml, mix_yaml, seed=42)               -> roster DataFrame
* generate_shift_log(workforce_df, start_date, end_date, env_df)  -> one row per scheduled shift
* daily_site_rollup(shift_log_df)                                 -> per site, per day metrics

Time handling: every timestamp is UTC (``shift_start_utc``). ``shift_date`` is the
Asia/Dubai calendar date on which the shift starts. It is a civil-date label (the
"rota day"), not an instant.

Reproducibility: every random draw comes from numpy Generators seeded by
(seed, site, [day]), so a given seed gives identical output. Extending the date
range does not change rows for days that were already generated.
"""
from __future__ import annotations

import warnings
import zlib
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

try:  # package import (pipelines) or flat import (tests / scripts)
    from .sites import MIX_YAML, SITES_YAML, load_sites, load_yaml
except ImportError:  # pragma: no cover
    from sites import MIX_YAML, SITES_YAML, load_sites, load_yaml  # type: ignore

SYNTHETIC_NOTICE = ("Synthetic workforce — demographically modelled on public aggregates; "
                    "not real workers. Not for individual employment decisions.")
DUBAI = ZoneInfo("Asia/Dubai")

NATIONALITIES = ["Indian", "Pakistani", "Bangladeshi", "Nepali", "Filipino", "Egyptian",
                 "Sudanese", "Other"]
ROLES = ["laborer", "foreman", "electrician", "welder", "crane_operator", "driver", "admin",
         "safety_officer"]
SHIFT_TYPES = ["morning", "midday", "evening", "night"]
ABSENCE_REASONS = ["none", "heat", "respiratory", "injury", "family", "other"]

WORKFORCE_COLUMNS = ["worker_id", "site_id", "nationality", "role", "age", "gender",
                     "ppe_compliance_baseline", "start_date",
                     # scheduling metadata used by generate_shift_log
                     "sector", "heat_exposure", "shift_pattern", "shift_type", "rest_weekday"]
SHIFT_LOG_COLUMNS = ["worker_id", "site_id", "shift_date", "shift_type", "shift_start_utc",
                     "scheduled_hours", "worked_hours", "absence_reason", "ppe_compliance_actual"]
ROLLUP_COLUMNS = ["site_id", "date", "scheduled_count", "worked_count", "absence_rate",
                  "heat_absence_rate", "respiratory_absence_rate", "mean_ppe_compliance",
                  "peak_absence_hour", "scheduled_hours", "worked_hours"]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _key(s: str) -> int:
    return zlib.crc32(s.encode())


def _weights(table: dict, keys: list[str]) -> np.ndarray:
    w = np.array([float(table.get(k, 0.0)) for k in keys])
    if w.sum() <= 0:
        raise ValueError(f"weights for {keys} sum to zero")
    return w / w.sum()


def _nationality_table(mix: dict, sector: str) -> dict:
    nat = mix["nationality"]
    t = (nat.get("by_sector") or {}).get(sector, "default")
    return nat["default"] if t == "default" else t


def _truncnorm_int(rng: np.random.Generator, n: int, mean: float, sd: float,
                   lo: float, hi: float) -> np.ndarray:
    out = np.empty(0)
    while out.size < n:
        x = rng.normal(mean, sd, size=max(2 * (n - out.size), 16))
        out = np.concatenate([out, x[(x >= lo) & (x <= hi)]])
    return np.rint(out[:n]).astype(int).clip(int(lo), int(hi))


def _logit(p: float) -> float:
    return float(np.log(p / (1.0 - p)))


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def _pos(x: np.ndarray) -> np.ndarray:
    return np.clip(np.nan_to_num(x, nan=0.0), 0.0, None)


# ---------------------------------------------------------------------------
# 1. Roster
# ---------------------------------------------------------------------------

def generate_workforce(sites_yaml: Path | str | pd.DataFrame = SITES_YAML,
                       mix_yaml: Path | str | dict = MIX_YAML,
                       seed: int = 42) -> pd.DataFrame:
    """One row per synthetic worker across all sites.

    Columns: worker_id, site_id, nationality, role, age, gender,
    ppe_compliance_baseline (0–1, biased by role), start_date, plus the scheduling
    metadata used by generate_shift_log (sector, heat_exposure, shift_pattern,
    shift_type, rest_weekday 0=Mon..6=Sun).
    """
    sites = load_sites(sites_yaml)
    mix = mix_yaml if isinstance(mix_yaml, dict) else load_yaml(mix_yaml)
    roster_as_of = date.fromisoformat(str(mix["roster_as_of"]))
    tenure_mean = float(mix["tenure_mean_years"]) * 365.25
    tenure_max = float(mix["tenure_max_years"]) * 365.25
    age = mix["age"]
    ppe = mix["ppe_baseline"]
    k = float(ppe["concentration"])

    frames = []
    for s in sites.itertuples():
        rng = np.random.default_rng([seed, _key(s.site_id)])
        n = int(s.worker_count)
        nat = rng.choice(NATIONALITIES, size=n, p=_weights(_nationality_table(mix, s.sector),
                                                           NATIONALITIES))
        role = rng.choice(ROLES, size=n, p=_weights(mix["role"][s.sector], ROLES))
        ages = _truncnorm_int(rng, n, age["mean"], age["sd"], age["min"], age["max"])
        male = rng.random(n) < float(mix["gender_male_share"][s.sector])
        means = np.array([ppe["by_role"][r] for r in role], dtype=float)
        ppe_base = rng.beta(means * k, (1.0 - means) * k).clip(0.0, 1.0)
        tenure = np.minimum(rng.exponential(tenure_mean, size=n), tenure_max).astype(int)
        start = [roster_as_of - timedelta(days=int(t)) for t in tenure]

        shifts = mix["shifts"][s.shift_pattern]
        names = list(shifts)
        slot = rng.choice(names, size=n, p=_weights({k_: v["share"] for k_, v in shifts.items()},
                                                    names))
        rest = rng.integers(0, 7, size=n)
        # Most site rotas give Friday off; the rest spread across the week.
        rest = np.where(rng.random(n) < 0.6, 4, rest)

        frames.append(pd.DataFrame({
            "worker_id": [f"{s.site_id}-{i:05d}" for i in range(n)],
            "site_id": s.site_id,
            "nationality": nat,
            "role": role,
            "age": ages,
            "gender": np.where(male, "male", "female"),
            "ppe_compliance_baseline": ppe_base.round(4),
            "start_date": start,
            "sector": s.sector,
            "heat_exposure": s.heat_exposure,
            "shift_pattern": s.shift_pattern,
            "shift_type": slot,
            "rest_weekday": rest.astype(int),
        }))
    wf = pd.concat(frames, ignore_index=True)[WORKFORCE_COLUMNS]
    for c in ("site_id", "nationality", "role", "gender", "sector", "heat_exposure",
              "shift_pattern", "shift_type"):
        wf[c] = wf[c].astype("category")
    return wf


# ---------------------------------------------------------------------------
# 2. Shift log
# ---------------------------------------------------------------------------

def _env_by_site(env: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """site_id -> hourly frame indexed by naive Asia/Dubai local time."""
    need = {"site_id", "ts_utc", "apparent_temp", "pm25", "pm10"}
    missing = need - set(env.columns)
    if missing:
        raise ValueError(f"env_signals_df missing columns {sorted(missing)}")
    e = env[list(need)].copy()
    ts = pd.to_datetime(e["ts_utc"], utc=True)
    e["local"] = ts.dt.tz_convert(DUBAI).dt.tz_localize(None)
    out = {}
    for sid, g in e.groupby("site_id", observed=True):
        out[str(sid)] = (g.drop_duplicates("local", keep="last").set_index("local")
                          .sort_index()[["apparent_temp", "pm25", "pm10"]])
    return out


def _shift_exposure(env: pd.DataFrame | None, days: pd.DatetimeIndex, start_hour: int,
                    hours: int, break_hours: set[int], in_break_season: np.ndarray):
    """Per-day exposure for one shift slot: (AT peak over worked hours, pm25 mean, pm10 mean)."""
    nd = len(days)
    if env is None or env.empty:
        nan = np.full(nd, np.nan)
        return nan, nan.copy(), nan.copy()
    offsets = np.arange(hours)
    idx = (days.values[:, None] + np.timedelta64(start_hour, "h")
           + offsets[None, :].astype("timedelta64[h]"))
    flat = pd.DatetimeIndex(idx.ravel())
    sub = env.reindex(flat)
    at = sub["apparent_temp"].to_numpy(float).reshape(nd, hours)
    pm25 = sub["pm25"].to_numpy(float).reshape(nd, hours)
    pm10 = sub["pm10"].to_numpy(float).reshape(nd, hours)
    # Outdoor work pauses during the MoHRE midday break: drop those hours from heat exposure.
    local_hour = (start_hour + offsets) % 24
    brk = np.isin(local_hour, list(break_hours))[None, :] & in_break_season[:, None]
    at = np.where(brk, np.nan, at)
    with warnings.catch_warnings():   # all-NaN rows (missing env) -> NaN, no warning
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanmax(at, axis=1), np.nanmean(pm25, axis=1), np.nanmean(pm10, axis=1)


def absence_probabilities(apparent_temp, pm25, pm10, heat_applies: bool, model: dict):
    """(p_heat, p_respiratory) arrays for the given shift exposures. See
    configs/workforce_mix.yaml `absence_model` for how the brief's equations are read."""
    at, p25, p10 = (np.asarray(x, dtype=float) for x in (apparent_temp, pm25, pm10))
    gated = bool(model.get("gated", True))

    h = model["heat"]
    h_at = _pos(at - h["apparent_temp_threshold"])
    h_pm = _pos(p25 - h["pm25_threshold"]) / 10.0
    eta = _logit(h["intercept_prob"]) + h["apparent_temp_slope"] * h_at + h["pm25_slope_per_10"] * h_pm
    p_heat = _sigmoid(eta)
    if gated:
        p_heat = np.where((h_at > 0) | (h_pm > 0), p_heat, 0.0)
    if not heat_applies:
        p_heat = np.zeros_like(p_heat)

    r = model["respiratory"]
    r25 = _pos(p25 - r["pm25_threshold"]) / 10.0
    r10 = _pos(p10 - r["pm10_threshold"]) / 20.0
    eta = _logit(r["intercept_prob"]) + r["pm25_slope_per_10"] * r25 + r["pm10_slope_per_20"] * r10
    p_resp = _sigmoid(eta)
    if gated:
        p_resp = np.where((r25 > 0) | (r10 > 0), p_resp, 0.0)
    return p_heat, p_resp


def _break_overlap(start_hour: float, hours: float, lo: float, hi: float) -> float:
    return max(0.0, min(start_hour + hours, hi) - max(start_hour, lo))


def generate_shift_log(workforce_df: pd.DataFrame, start_date: date, end_date: date,
                       env_signals_df: pd.DataFrame, *, mix_yaml: Path | str | dict = MIX_YAML,
                       seed: int = 42) -> pd.DataFrame:
    """One row per scheduled worker-shift between start_date and end_date (inclusive,
    Asia/Dubai rota days).

    ``env_signals_df`` is hourly per site: site_id, ts_utc (UTC), apparent_temp (°C),
    pm25, pm10 (µg/m³), e.g. data/processed/env/site_hourly.parquet.

    Absence and PPE respond to the environment over each shift's working hours:

    * heat absence (outdoor and mixed sites only) rises with peak apparent temperature
      above 38 °C and PM2.5 above 75
    * respiratory absence rises with PM2.5 above 55 and PM10 above 100
    * PPE compliance drops 0.15 per 10 °C of apparent temperature above 42
    * non-environmental baseline absence is 2% (family / other / injury)

    Missing environmental hours contribute no excess (the baseline only).
    """
    if start_date > end_date:
        raise ValueError("start_date must be <= end_date")
    mix = mix_yaml if isinstance(mix_yaml, dict) else load_yaml(mix_yaml)
    model = mix["absence_model"]
    shifts_cfg = mix["shifts"]
    mb = mix["midday_break"]
    brk_lo, brk_hi = float(mb["from_hour"]), float(mb["to_hour"])
    brk_hours = {h for h in range(24) if brk_lo <= h < brk_hi}   # hourly samples in window
    ppe_cfg = model["ppe"]
    base = model["baseline"]
    p_injury, p_family, p_other = float(base["injury"]), float(base["family"]), float(base["other"])

    days = pd.date_range(start_date, end_date, freq="D")
    mmdd = days.strftime("%m-%d")
    in_season = np.asarray((mmdd >= mb["start"]) & (mmdd <= mb["end"]))
    weekday = days.day_of_week.to_numpy()
    env_sites = _env_by_site(env_signals_df) if env_signals_df is not None else {}

    out: dict[str, list] = {c: [] for c in SHIFT_LOG_COLUMNS}
    for sid, wf in workforce_df.groupby("site_id", observed=True, sort=True):
        sid = str(sid)
        wf = wf.reset_index(drop=True)
        exposure = str(wf["heat_exposure"].iloc[0])
        pattern = str(wf["shift_pattern"].iloc[0])
        heat_applies = exposure in model["heat"]["exposures"]
        ppe_applies = exposure in ppe_cfg["exposures"]
        break_applies = exposure in mb["applies_to"]
        slots = list(shifts_cfg[pattern])
        slot_idx = wf["shift_type"].astype(str).map({s: i for i, s in enumerate(slots)}).to_numpy()

        # Per-slot, per-day exposure and probabilities: arrays (n_slots, n_days).
        n_slots, nd = len(slots), len(days)
        p_heat = np.zeros((n_slots, nd))
        p_resp = np.zeros((n_slots, nd))
        at_peak = np.full((n_slots, nd), np.nan)
        sched_h = np.zeros((n_slots, nd))
        start_utc = np.empty((n_slots, nd), dtype="datetime64[ns]")
        env = env_sites.get(sid)
        for i, slot in enumerate(slots):
            cfg = shifts_cfg[pattern][slot]
            sh, hrs = int(cfg["start_hour"]), int(cfg["hours"])
            season = in_season & break_applies
            at, p25, p10 = _shift_exposure(env, days, sh, hrs, brk_hours, season)
            at_peak[i] = at
            p_heat[i], p_resp[i] = absence_probabilities(at, p25, p10, heat_applies, model)
            cut = _break_overlap(sh, hrs, brk_lo, brk_hi)
            sched_h[i] = np.where(season, hrs - cut, hrs)
            local = days + pd.Timedelta(hours=sh)
            start_utc[i] = (local.tz_localize(DUBAI).tz_convert("UTC").tz_localize(None)
                            .to_numpy())

        ppe_base = wf["ppe_compliance_baseline"].to_numpy(float)
        start_ok = pd.to_datetime(wf["start_date"]).to_numpy()
        rest = wf["rest_weekday"].to_numpy()
        wid = wf["worker_id"].to_numpy()

        for d in range(nd):
            rng = np.random.default_rng([seed, _key(sid), days[d].toordinal()])
            u = rng.random(len(wf))
            noise = rng.normal(0.0, float(ppe_cfg["noise_sd"]), size=len(wf))
            on = (rest != weekday[d]) & (start_ok <= days[d].to_datetime64())
            if not on.any():
                continue
            s_i = slot_idx[on]
            ph, pr = p_heat[s_i, d], p_resp[s_i, d]
            c1 = ph
            c2 = c1 + pr
            c3 = c2 + p_injury
            c4 = c3 + p_family
            c5 = c4 + p_other
            uu = u[on]
            reason = np.select([uu < c1, uu < c2, uu < c3, uu < c4, uu < c5],
                               ["heat", "respiratory", "injury", "family", "other"], "none")
            present = reason == "none"
            sh_hours = sched_h[s_i, d]
            at = at_peak[s_i, d]
            drop = (ppe_cfg["drop_per_10c"] * _pos(at - ppe_cfg["apparent_temp_threshold"]) / 10.0
                    if ppe_applies else 0.0)
            ppe = np.clip(ppe_base[on] - drop + noise[on], 0.0, 1.0)

            out["worker_id"].append(wid[on])
            out["site_id"].append(np.full(on.sum(), sid, dtype=object))
            out["shift_date"].append(np.full(on.sum(), days[d].date(), dtype=object))
            out["shift_type"].append(np.asarray(slots, dtype=object)[s_i])
            out["shift_start_utc"].append(start_utc[s_i, d])
            out["scheduled_hours"].append(sh_hours)
            out["worked_hours"].append(np.where(present, sh_hours, 0.0))
            out["absence_reason"].append(reason)
            out["ppe_compliance_actual"].append(np.where(present, ppe.round(4), np.nan))

    if not out["worker_id"]:
        return pd.DataFrame(columns=SHIFT_LOG_COLUMNS)
    log = pd.DataFrame({c: np.concatenate(v) for c, v in out.items()})
    log["shift_start_utc"] = pd.to_datetime(log["shift_start_utc"]).dt.tz_localize("UTC")
    log["scheduled_hours"] = log["scheduled_hours"].astype("float32")
    log["worked_hours"] = log["worked_hours"].astype("float32")
    log["ppe_compliance_actual"] = log["ppe_compliance_actual"].astype("float32")
    log["site_id"] = log["site_id"].astype("category")
    log["shift_type"] = pd.Categorical(log["shift_type"], categories=SHIFT_TYPES)
    log["absence_reason"] = pd.Categorical(log["absence_reason"], categories=ABSENCE_REASONS)
    return log[SHIFT_LOG_COLUMNS]


# ---------------------------------------------------------------------------
# 3. Daily rollup
# ---------------------------------------------------------------------------

def daily_site_rollup(shift_log_df: pd.DataFrame) -> pd.DataFrame:
    """Per site, per rota day (Asia/Dubai) metrics.

    Columns: site_id, date, scheduled_count, worked_count, absence_rate,
    heat_absence_rate, respiratory_absence_rate, mean_ppe_compliance (present
    workers), peak_absence_hour (Asia/Dubai start hour of the shift with the most
    absences that day; null if none), scheduled_hours, worked_hours.
    """
    log = shift_log_df
    if log.empty:
        return pd.DataFrame(columns=ROLLUP_COLUMNS)
    reason = log["absence_reason"].astype(str)
    df = pd.DataFrame({
        "site_id": log["site_id"].astype(str),
        "date": log["shift_date"],
        "present": (reason == "none").to_numpy(),
        "heat": (reason == "heat").to_numpy(),
        "resp": (reason == "respiratory").to_numpy(),
        "ppe": log["ppe_compliance_actual"].astype(float),
        "sh": log["scheduled_hours"].astype(float),
        "wh": log["worked_hours"].astype(float),
    })
    g = df.groupby(["site_id", "date"], sort=True)
    r = g.agg(scheduled_count=("present", "size"), worked_count=("present", "sum"),
              heat=("heat", "sum"), resp=("resp", "sum"), mean_ppe_compliance=("ppe", "mean"),
              scheduled_hours=("sh", "sum"), worked_hours=("wh", "sum")).reset_index()
    r["absence_rate"] = 1.0 - r["worked_count"] / r["scheduled_count"]
    r["heat_absence_rate"] = r["heat"] / r["scheduled_count"]
    r["respiratory_absence_rate"] = r["resp"] / r["scheduled_count"]

    absent = df.loc[~df["present"], ["site_id", "date"]].copy()
    absent["hour"] = (log.loc[~df["present"].to_numpy(), "shift_start_utc"]
                      .dt.tz_convert(DUBAI).dt.hour.to_numpy())
    if absent.empty:
        r["peak_absence_hour"] = pd.array([pd.NA] * len(r), dtype="Int64")
    else:
        c = absent.groupby(["site_id", "date", "hour"]).size().rename("n").reset_index()
        c = c.sort_values(["site_id", "date", "n", "hour"], ascending=[True, True, False, True])
        peak = c.drop_duplicates(["site_id", "date"]).set_index(["site_id", "date"])["hour"]
        r["peak_absence_hour"] = pd.array(
            [peak.get((s, d), pd.NA) for s, d in zip(r["site_id"], r["date"])], dtype="Int64")

    for col in ("absence_rate", "heat_absence_rate", "respiratory_absence_rate",
                "mean_ppe_compliance"):
        r[col] = r[col].astype(float).round(5)
    r["scheduled_count"] = r["scheduled_count"].astype(int)
    r["worked_count"] = r["worked_count"].astype(int)
    r["scheduled_hours"] = r["scheduled_hours"].round(1)
    r["worked_hours"] = r["worked_hours"].round(1)
    return r[ROLLUP_COLUMNS]
