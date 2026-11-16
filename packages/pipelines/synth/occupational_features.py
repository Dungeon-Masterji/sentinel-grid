"""Alert-input view for the occupational pillar.

build_feature_frame() joins

    data/processed/env/site_hourly.parquet                  (hourly env per site)
    data/processed/occupational/shift_log.parquet           (who was scheduled and present, when)
    data/processed/occupational/daily_site_rollup.parquet   (per site, per rota day)
    data/processed/occupational/workforce.parquet           (roster metadata)

into one frame keyed by (site_id, ts_utc) with columns:

    site_id, ts_utc, date, hour      date/hour are the UTC calendar date/hour of ts_utc
    apparent_temp, pm25, pm10
    workers_on_site                  present workers whose shift covers this hour
    scheduled_on_site                workers rostered for a shift covering this hour
    absence_rate_last_1h             1 - workers_on_site / scheduled_on_site
    ppe_compliance                   mean PPE compliance of present workers this hour
    heat_exposure, shift_type        dominant shift (most scheduled workers) this hour
    roster_size                      site headcount from the roster
    day_absence_rate                 rota-day absence rate from daily_site_rollup

and writes it to data/processed/features/occupational.parquet. Everything is
stored in UTC; the API converts to Asia/Dubai. All worker data is synthetic.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from packages.pipelines.synth.sites import REPO_ROOT

DUBAI = ZoneInfo("Asia/Dubai")
OCC_DIR = REPO_ROOT / "data" / "processed" / "occupational"
ENV_PATH = REPO_ROOT / "data" / "processed" / "env" / "site_hourly.parquet"
FEATURES_PATH = REPO_ROOT / "data" / "processed" / "features" / "occupational.parquet"
FEATURE_COLUMNS = ["site_id", "ts_utc", "date", "hour", "apparent_temp", "pm25", "pm10",
                   "workers_on_site", "scheduled_on_site", "absence_rate_last_1h",
                   "ppe_compliance", "heat_exposure", "shift_type", "roster_size",
                   "day_absence_rate"]


def shift_hours_frame(shift_log: pd.DataFrame, shift_lengths: dict[str, int]) -> pd.DataFrame:
    """Expand per-shift aggregates to one row per (site, hour, shift_type)."""
    present = (shift_log["absence_reason"].astype(str) == "none")
    agg = (pd.DataFrame({
        "site_id": shift_log["site_id"].astype(str),
        "shift_start_utc": shift_log["shift_start_utc"],
        "shift_type": shift_log["shift_type"].astype(str),
        "present": present.to_numpy(),
        "ppe": shift_log["ppe_compliance_actual"].astype(float).fillna(0.0),
    }).groupby(["site_id", "shift_start_utc", "shift_type"], sort=False)
      .agg(scheduled=("present", "size"), present=("present", "sum"), ppe_sum=("ppe", "sum"))
      .reset_index())
    agg["len"] = agg["shift_type"].map(shift_lengths).fillna(8).astype(int)
    rep = agg.loc[agg.index.repeat(agg["len"])].copy()
    rep["k"] = rep.groupby(level=0).cumcount()
    rep["ts_utc"] = rep["shift_start_utc"] + pd.to_timedelta(rep["k"], unit="h")
    return rep[["site_id", "ts_utc", "shift_type", "scheduled", "present", "ppe_sum"]]


def build_feature_frame(site_hourly: pd.DataFrame | None = None,
                        daily_rollup: pd.DataFrame | None = None,
                        workforce: pd.DataFrame | None = None,
                        shift_log: pd.DataFrame | None = None, *,
                        mix_yaml: Path | None = None,
                        out_path: Path | None = FEATURES_PATH) -> pd.DataFrame:
    from packages.pipelines.synth.sites import MIX_YAML, load_yaml
    site_hourly = site_hourly if site_hourly is not None else pd.read_parquet(ENV_PATH)
    daily_rollup = (daily_rollup if daily_rollup is not None
                    else pd.read_parquet(OCC_DIR / "daily_site_rollup.parquet"))
    workforce = workforce if workforce is not None else pd.read_parquet(OCC_DIR / "workforce.parquet")
    shift_log = shift_log if shift_log is not None else pd.read_parquet(OCC_DIR / "shift_log.parquet")
    mix_shifts = load_yaml(mix_yaml or MIX_YAML)["shifts"]

    # Shift lengths depend on the site's pattern (a 12h and an 8h night shift share a name).
    site_meta = (workforce.groupby("site_id", observed=True)
                 .agg(heat_exposure=("heat_exposure", "first"),
                      shift_pattern=("shift_pattern", "first"),
                      roster_size=("worker_id", "size")).reset_index())
    site_meta["site_id"] = site_meta["site_id"].astype(str)
    site_meta["heat_exposure"] = site_meta["heat_exposure"].astype(str)
    site_meta["shift_pattern"] = site_meta["shift_pattern"].astype(str)

    parts = []
    for pattern, g in shift_log.assign(site_id=shift_log["site_id"].astype(str)).merge(
            site_meta[["site_id", "shift_pattern"]], on="site_id").groupby("shift_pattern"):
        lens = {k: int(v["hours"]) for k, v in mix_shifts[pattern].items()}
        parts.append(shift_hours_frame(g, lens))
    sh = pd.concat(parts, ignore_index=True)

    per_hour = (sh.groupby(["site_id", "ts_utc"])
                  .agg(scheduled_on_site=("scheduled", "sum"), workers_on_site=("present", "sum"),
                       ppe_sum=("ppe_sum", "sum")).reset_index())
    dominant = (sh.sort_values(["site_id", "ts_utc", "scheduled"], ascending=[True, True, False])
                  .drop_duplicates(["site_id", "ts_utc"])[["site_id", "ts_utc", "shift_type"]])
    per_hour = per_hour.merge(dominant, on=["site_id", "ts_utc"], how="left")

    env = site_hourly[["site_id", "ts_utc", "apparent_temp", "pm25", "pm10"]].copy()
    env["site_id"] = env["site_id"].astype(str)
    df = env.merge(per_hour, on=["site_id", "ts_utc"], how="left")
    df["workers_on_site"] = df["workers_on_site"].fillna(0).astype(int)
    df["scheduled_on_site"] = df["scheduled_on_site"].fillna(0).astype(int)
    with np.errstate(invalid="ignore", divide="ignore"):
        df["absence_rate_last_1h"] = np.where(
            df["scheduled_on_site"] > 0,
            1.0 - df["workers_on_site"] / df["scheduled_on_site"].replace(0, np.nan), np.nan)
        df["ppe_compliance"] = np.where(df["workers_on_site"] > 0,
                                        df["ppe_sum"] / df["workers_on_site"].replace(0, np.nan),
                                        np.nan)
    df["shift_type"] = df["shift_type"].fillna("off")
    df = df.merge(site_meta[["site_id", "heat_exposure", "roster_size"]], on="site_id", how="left")

    # rota day = Asia/Dubai date of the hour (for the day-level join only)
    roll = daily_rollup[["site_id", "date", "absence_rate"]].rename(
        columns={"date": "rota_day", "absence_rate": "day_absence_rate"}).copy()
    roll["site_id"] = roll["site_id"].astype(str)
    roll["rota_day"] = pd.to_datetime(roll["rota_day"]).dt.date
    df["rota_day"] = df["ts_utc"].dt.tz_convert(DUBAI).dt.date
    df = df.merge(roll, on=["site_id", "rota_day"], how="left").drop(columns=["rota_day"])

    df["date"] = df["ts_utc"].dt.date
    df["hour"] = df["ts_utc"].dt.hour.astype("int8")
    df["absence_rate_last_1h"] = df["absence_rate_last_1h"].round(5)
    df["ppe_compliance"] = df["ppe_compliance"].round(4)
    df = df[FEATURE_COLUMNS].sort_values(["site_id", "ts_utc"]).reset_index(drop=True)

    if out_path is not None:
        write_parquet(df, Path(out_path))
    return df


def write_parquet(df: pd.DataFrame, path: Path, notice: str | None = None) -> None:
    """Atomic zstd parquet write tagged as synthetic."""
    import os
    import pyarrow as pa
    import pyarrow.parquet as pq
    from packages.pipelines.synth.workforce import SYNTHETIC_NOTICE
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(df, preserve_index=False)
    meta = {b"synthetic_notice": (notice or SYNTHETIC_NOTICE).encode(),
            b"generated_at": datetime.now(timezone.utc).isoformat().encode()}
    table = table.replace_schema_metadata({**(table.schema.metadata or {}), **meta})
    tmp = path.with_suffix(path.suffix + ".tmp")
    pq.write_table(table, tmp, compression="zstd")
    os.replace(tmp, path)
