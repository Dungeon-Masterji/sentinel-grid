"""Hourly environmental signals for every occupational site.

    python -m packages.pipelines.synth.env_join --start 2026-08-18 --end 2026-11-16

pull_env_for_sites() writes data/processed/env/site_hourly.parquet with one row per
(site_id, ts_utc) hour:

    site_id, ts_utc (UTC), temperature_2m, relative_humidity_2m, apparent_temp,
    wind_speed_10m, precipitation, pm25, pm10, met_source, pm_source

Sources, by priority:

* Meteorology: Open-Meteo **archive** API (ERA5 reanalysis). It lags real time by a
  few days, so the Open-Meteo **forecast** API (same variables, which also serves
  recent past days) fills hours the archive does not have yet, including today.
* Particulates (µg/m³): WAQI has no public historical API, so hourly history comes
  from the Open-Meteo **air-quality** API (CAMS). Hours CAMS cannot provide fall back
  to the current reading of the nearest WAQI station (the same upstream as
  apps/api/routers/signals.py). WAQI reports US-AQI sub-indices, which are converted
  back to concentrations with the EPA breakpoints. That fallback is a single
  current value per site, flagged pm_source="waqi_current".

Idempotent: rows for (site_id, ts_utc) are replaced, not appended. Re-running for an
overlapping range gives the same file for the same upstream data. The write is
atomic (temp file + rename).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from packages.pipelines.synth.sites import REPO_ROOT, SITES_YAML, load_sites  # noqa: E402

DUBAI = ZoneInfo("Asia/Dubai")
OUT_PATH = REPO_ROOT / "data" / "processed" / "env" / "site_hourly.parquet"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
AIR_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
WAQI_URL = "https://api.waqi.info/feed/geo:{lat};{lon}/"
MET_VARS = ["temperature_2m", "relative_humidity_2m", "apparent_temperature",
            "wind_speed_10m", "precipitation"]
COLUMNS = ["site_id", "ts_utc", "temperature_2m", "relative_humidity_2m", "apparent_temp",
           "wind_speed_10m", "precipitation", "pm25", "pm10", "met_source", "pm_source"]
ARCHIVE_LAG_DAYS = 6          # ERA5 availability; the forecast API covers the gap
FORECAST_MAX_PAST_DAYS = 92   # Open-Meteo forecast API history limit
USER_AGENT = "sentinel-grid-pipeline/0.3"

# US EPA AQI breakpoints (2024 PM2.5 revision): (C_lo, C_hi, I_lo, I_hi)
_PM25_BP = [(0.0, 9.0, 0, 50), (9.1, 35.4, 51, 100), (35.5, 55.4, 101, 150),
            (55.5, 125.4, 151, 200), (125.5, 225.4, 201, 300), (225.5, 325.4, 301, 500)]
_PM10_BP = [(0, 54, 0, 50), (55, 154, 51, 100), (155, 254, 101, 150), (255, 354, 151, 200),
            (355, 424, 201, 300), (425, 604, 301, 500)]


def aqi_to_concentration(aqi: float | None, pollutant: str) -> float | None:
    """Invert a US-AQI sub-index back to µg/m³ (linear within each band)."""
    if aqi is None or not np.isfinite(aqi):
        return None
    bps = _PM25_BP if pollutant == "pm25" else _PM10_BP
    for c_lo, c_hi, i_lo, i_hi in bps:
        if aqi <= i_hi:
            return round(c_lo + (aqi - i_lo) * (c_hi - c_lo) / (i_hi - i_lo), 1)
    return float(bps[-1][1])


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _get(url: str, params: dict, *, session: requests.Session, tries: int = 4) -> dict | list:
    last = None
    for i in range(tries):
        try:
            r = session.get(url, params=params, timeout=60)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"{r.status_code}: {r.text[:200]}")
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:  # noqa: PERF203
            last = e
            time.sleep(2 ** i)
    raise RuntimeError(f"GET {url} failed after {tries} tries: {last}")


def _open_meteo_frame(payload: dict | list, sites: pd.DataFrame, rename: dict) -> pd.DataFrame:
    """Multi-location Open-Meteo response -> long frame (site_id, ts_utc, vars...)."""
    items = payload if isinstance(payload, list) else [payload]
    frames = []
    for sid, item in zip(sites["site_id"], items):
        h = item.get("hourly") or {}
        if not h.get("time"):
            continue
        f = pd.DataFrame(h)
        f["ts_utc"] = pd.to_datetime(f.pop("time")).dt.tz_localize("UTC")
        f["site_id"] = sid
        frames.append(f.rename(columns=rename))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _om_params(sites: pd.DataFrame, start: date, end: date, hourly: list[str]) -> dict:
    return {"latitude": ",".join(f"{x:.5f}" for x in sites["lat"]),
            "longitude": ",".join(f"{x:.5f}" for x in sites["lon"]),
            "start_date": start.isoformat(), "end_date": end.isoformat(),
            "hourly": ",".join(hourly), "timezone": "GMT", "wind_speed_unit": "kmh"}


def fetch_meteo(sites: pd.DataFrame, start_utc: date, end_utc: date, *,
                session: requests.Session, today_utc: date) -> pd.DataFrame:
    rename = {"apparent_temperature": "apparent_temp"}
    parts = []
    arch_end = min(end_utc, today_utc - timedelta(days=ARCHIVE_LAG_DAYS - 1))
    if start_utc <= arch_end:
        a = _open_meteo_frame(_get(ARCHIVE_URL, _om_params(sites, start_utc, arch_end, MET_VARS),
                                   session=session), sites, rename)
        if not a.empty:
            a["met_source"] = "open-meteo-archive"
            parts.append(a)
    fc_start = max(start_utc, today_utc - timedelta(days=FORECAST_MAX_PAST_DAYS - 1),
                   arch_end - timedelta(days=3))   # small overlap fills archive NaNs
    if fc_start <= end_utc:
        f = _open_meteo_frame(_get(FORECAST_URL, _om_params(sites, fc_start, end_utc, MET_VARS),
                                   session=session), sites, rename)
        if not f.empty:
            f["met_source"] = "open-meteo-forecast"
            parts.append(f)
    if not parts:
        return pd.DataFrame(columns=["site_id", "ts_utc"])
    met = pd.concat(parts, ignore_index=True)
    # Prefer archive rows with real values; forecast fills the rest.
    met["_has"] = met["apparent_temp"].notna()
    met["_pri"] = (met["met_source"] == "open-meteo-archive").astype(int)
    met = (met.sort_values(["site_id", "ts_utc", "_has", "_pri"])
              .drop_duplicates(["site_id", "ts_utc"], keep="last")
              .drop(columns=["_has", "_pri"]))
    return met


def fetch_air(sites: pd.DataFrame, start_utc: date, end_utc: date, *,
              session: requests.Session) -> pd.DataFrame:
    try:
        payload = _get(AIR_URL, {**_om_params(sites, start_utc, end_utc, ["pm2_5", "pm10"]),
                                 "domains": "cams_global"}, session=session)
    except RuntimeError as e:
        print(f"  air-quality API unavailable ({e}); will use WAQI fallback", file=sys.stderr)
        return pd.DataFrame(columns=["site_id", "ts_utc", "pm25", "pm10"])
    air = _open_meteo_frame(payload, sites, {"pm2_5": "pm25"})
    if not air.empty:
        air["pm_source"] = np.where(air["pm25"].notna(), "open-meteo-cams", None)
    return air


def waqi_current(sites: pd.DataFrame, *, session: requests.Session,
                 token: str | None) -> pd.DataFrame:
    """Nearest-station current PM per site (µg/m³). Empty frame without a token."""
    rows = []
    if not token:
        return pd.DataFrame(columns=["site_id", "pm25_waqi", "pm10_waqi"])
    for s in sites.itertuples():
        try:
            r = _get(WAQI_URL.format(lat=s.lat, lon=s.lon), {"token": token}, session=session,
                     tries=2)
        except RuntimeError:
            continue
        if not isinstance(r, dict) or r.get("status") != "ok":
            continue
        iaqi = r["data"].get("iaqi", {})
        rows.append({"site_id": s.site_id,
                     "pm25_waqi": aqi_to_concentration(iaqi.get("pm25", {}).get("v"), "pm25"),
                     "pm10_waqi": aqi_to_concentration(iaqi.get("pm10", {}).get("v"), "pm10")})
    return pd.DataFrame(rows, columns=["site_id", "pm25_waqi", "pm10_waqi"])


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def _local_range_to_utc_dates(start: date, end: date) -> tuple[date, date, pd.Timestamp,
                                                                pd.Timestamp]:
    lo = pd.Timestamp(datetime.combine(start, datetime.min.time()), tz=DUBAI).tz_convert("UTC")
    hi = (pd.Timestamp(datetime.combine(end + timedelta(days=1), datetime.min.time()), tz=DUBAI)
          .tz_convert("UTC") - pd.Timedelta(hours=1))
    return lo.date(), hi.date(), lo, hi


def _write_atomic(df: pd.DataFrame, path: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(df, preserve_index=False)
    meta = {b"generated_at": datetime.now(timezone.utc).isoformat().encode(),
            b"sources": b"open-meteo archive/forecast/air-quality (CAMS); WAQI current fallback"}
    table = table.replace_schema_metadata({**(table.schema.metadata or {}), **meta})
    tmp = path.with_suffix(".parquet.tmp")
    pq.write_table(table, tmp, compression="zstd")
    os.replace(tmp, path)


def merge_hourly(existing: pd.DataFrame | None, new: pd.DataFrame) -> pd.DataFrame:
    """Replace (site_id, ts_utc) rows of ``existing`` with ``new``; sorted and deduped."""
    frames = [new] if existing is None or existing.empty else [existing, new]
    df = pd.concat(frames, ignore_index=True)
    df = (df.drop_duplicates(["site_id", "ts_utc"], keep="last")
            .sort_values(["site_id", "ts_utc"]).reset_index(drop=True))
    return df[COLUMNS]


def pull_env_for_sites(sites: pd.DataFrame | Path | str = SITES_YAML, start_date: date | None = None,
                       end_date: date | None = None, *, out_path: Path = OUT_PATH,
                       waqi_token: str | None = None,
                       session: requests.Session | None = None) -> pd.DataFrame:
    """Fetch hourly met + PM for each site between two Asia/Dubai dates (inclusive),
    merge into ``out_path`` and return the rows for the requested range."""
    sites = load_sites(sites)
    today_local = datetime.now(DUBAI).date()
    end_date = end_date or today_local
    start_date = start_date or end_date - timedelta(days=89)
    if start_date > end_date:
        raise ValueError("start_date must be <= end_date")
    s_utc, e_utc, lo, hi = _local_range_to_utc_dates(start_date, end_date)
    today_utc = datetime.now(timezone.utc).date()
    session = session or requests.Session()
    session.headers.setdefault("User-Agent", USER_AGENT)

    print(f"  open-meteo met: {len(sites)} sites, {s_utc} → {e_utc} UTC")
    met = fetch_meteo(sites, s_utc, e_utc, session=session, today_utc=today_utc)
    print(f"  open-meteo air quality (CAMS)")
    air = fetch_air(sites, s_utc, e_utc, session=session)

    grid = pd.MultiIndex.from_product(
        [sites["site_id"], pd.date_range(lo, hi, freq="h")], names=["site_id", "ts_utc"]
    ).to_frame(index=False)
    df = grid.merge(met, on=["site_id", "ts_utc"], how="left")
    if not air.empty:
        df = df.merge(air[["site_id", "ts_utc", "pm25", "pm10", "pm_source"]],
                      on=["site_id", "ts_utc"], how="left")
    else:
        df["pm25"], df["pm10"], df["pm_source"] = np.nan, np.nan, None

    if df["pm25"].isna().any():
        token = waqi_token if waqi_token is not None else _waqi_token()
        w = waqi_current(sites, session=session, token=token)
        if not w.empty:
            df = df.merge(w, on="site_id", how="left")
            fill = df["pm25"].isna() & df["pm25_waqi"].notna()
            df.loc[fill, "pm25"] = df.loc[fill, "pm25_waqi"]
            df.loc[fill & df["pm10"].isna(), "pm10"] = df.loc[fill, "pm10_waqi"]
            df.loc[fill, "pm_source"] = "waqi_current"
            df = df.drop(columns=["pm25_waqi", "pm10_waqi"])

    for c in COLUMNS:
        if c not in df.columns:
            df[c] = np.nan if c not in ("met_source", "pm_source") else None
    for c in ("temperature_2m", "relative_humidity_2m", "apparent_temp", "wind_speed_10m",
              "precipitation", "pm25", "pm10"):
        df[c] = df[c].astype("float64")
    df = df[COLUMNS]

    existing = pd.read_parquet(out_path) if Path(out_path).is_file() else None
    _write_atomic(merge_hourly(existing, df), Path(out_path))
    return df


def _waqi_token() -> str | None:
    tok = os.environ.get("WAQI_TOKEN")
    if tok:
        return tok
    env = REPO_ROOT / ".env"
    if env.is_file():
        for line in env.read_text().splitlines():
            if line.startswith("WAQI_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"') or None
    return None


def load_site_hourly(path: Path = OUT_PATH) -> pd.DataFrame:
    return pd.read_parquet(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", type=date.fromisoformat, default=None, help="Asia/Dubai date")
    ap.add_argument("--end", type=date.fromisoformat, default=None, help="Asia/Dubai date")
    ap.add_argument("--out", type=Path, default=OUT_PATH)
    args = ap.parse_args(argv)
    df = pull_env_for_sites(SITES_YAML, args.start, args.end, out_path=args.out)
    print(df.groupby("site_id")[["apparent_temp", "pm25", "pm10"]].agg(["mean", "max"]).round(1))
    print(f"wrote {args.out}  (pm_source: {df['pm_source'].value_counts(dropna=False).to_dict()})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
