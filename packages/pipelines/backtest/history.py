"""Historical signal panel for the backtest: real weather + synthetic people.

    pull_history(start, end)  ->  data/processed/backtest/{YYYY-MM}.parquet + units.parquet

Each monthly file is the long signal panel the detectors consume (fusion/signals.py:
signal, h3, ts_utc, value, n_records, suppressed) plus three provenance columns:

    observed_at   when the underlying observation happened (UTC)
    available_at  when the replay may see it (UTC): ts_utc + 1 h for every row
    source        where it came from

Sources
  env    Open-Meteo archive (ERA5) hourly met per site, Open-Meteo air-quality (CAMS
         global, from 2022-08) for PM2.5/PM10. WAQI has NO public historical API: its
         feed endpoint is current-only, so it cannot backfill and is recorded as a gap.
         OpenAQ v3 is used to fill hours CAMS is missing, only if $OPENAQ_API_KEY is set
         (v3 requires a key); without one those hours stay NaN (never imputed).
  occ    workforce.generate_shift_log driven by the historical site weather above
         (the same absence model as the live pipeline), seeded per month.
  clin   ONE Synthea run of the UAE population whose reference date is 2 days past the
         window (Synthea simulates whole lifetimes, so one run covers 2022-2024 and the
         end-of-run taper falls outside it), parsed with synthea_wrapper. The febrile
         module (gated to >= 2025 in the live config) is started at the window's first
         year for this run, so the infectious scenario has fever data to work with. Synthea's
         modules do not see real weather, so a modest weather coupling is added: on days
         whose max apparent temperature at the catchment city exceeds 40 °C, extra
         heat-related encounters are drawn (Poisson, +10 % of the city's mean daily rate
         per °C above 40, capped at +100 %); on days whose mean PM10 exceeds 100 µg/m³,
         extra respiratory encounters (+1 % per µg/m³ above 100, capped at +80 %). Each
         city-day has its own seed derived from the date, so files are reproducible.

Clinical lateness (the Chunk 1 taper): the replay treats the T-2 day as the latest
reliable clinical observation. Clinical rows are therefore shifted by +48 h: the value at
ts is the trailing-24 h count that ENDED at ts - 48 h, and observed_at says so.

Idempotent: months that already have a file are skipped; ``force=True`` rebuilds.
"""
from __future__ import annotations

import hashlib
import logging
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from packages.pipelines.fusion.config import SCENARIOS_YAML, load_scenarios
from packages.pipelines.fusion.signals import (CLIN_SIGNALS, PANEL_COLUMNS, build_signal_panel,
                                               build_units, load_cities)

log = logging.getLogger("sentinel.backtest.history")

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKTEST_DIR = REPO_ROOT / "data" / "processed" / "backtest"
RAW_DIR = REPO_ROOT / "data" / "raw" / "backtest"
HISTORY_COLUMNS = PANEL_COLUMNS + ["pillar", "observed_at", "available_at", "source"]
PUBLISH_LATENCY = pd.Timedelta(hours=1)
CLINICAL_LAG = pd.Timedelta(hours=48)
WARMUP_DAYS = 3          # trailing 24 h counts + night shifts need the days before a month
HISTORY_VERSION = 1

# weather coupling for the synthetic clinical pillar ("modest correlation")
HEAT_AT_THRESHOLD = 40.0
HEAT_BOOST_PER_C, HEAT_BOOST_CAP = 0.10, 1.0
DUST_PM10_THRESHOLD = 100.0
DUST_BOOST_PER_UG, DUST_BOOST_CAP = 0.01, 0.8

OPENAQ_URL = "https://api.openaq.org/v3"


def _seed(*parts) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:8], 16)


def months_between(start, end) -> list[pd.Period]:
    s, e = (pd.Timestamp(x).tz_localize(None) if pd.Timestamp(x).tzinfo else pd.Timestamp(x) for x in (start, end))
    return list(pd.period_range(s.to_period("M"), e.to_period("M"),
                                freq="M"))


def month_path(m: pd.Period, out_dir: Path = BACKTEST_DIR) -> Path:
    return Path(out_dir) / f"{m.strftime('%Y-%m')}.parquet"


# ---------------------------------------------------------------------------------------
# environmental
# ---------------------------------------------------------------------------------------
def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = "sentinel-grid-backtest/0.7"
    return s


def openaq_pm(points: pd.DataFrame, start: date, end: date, *, api_key: str,
              session: requests.Session, radius_m: int = 25_000) -> pd.DataFrame:
    """Hourly PM2.5/PM10 from the nearest OpenAQ v3 location to each point (needs a key)."""
    hdr = {"X-API-Key": api_key}
    rows = []
    for p in points.itertuples():
        r = session.get(f"{OPENAQ_URL}/locations", headers=hdr, timeout=60,
                        params={"coordinates": f"{p.lat:.4f},{p.lon:.4f}", "radius": radius_m,
                                "limit": 10})
        if r.status_code != 200:
            log.warning("OpenAQ locations %s: HTTP %s", p.site_id, r.status_code)
            continue
        sensors = {}
        for loc in r.json().get("results", []):
            for s in loc.get("sensors", []):
                name = (s.get("parameter") or {}).get("name")
                if name in ("pm25", "pm10") and name not in sensors:
                    sensors[name] = s["id"]
        for name, sid in sensors.items():
            page = 1
            while True:
                h = session.get(f"{OPENAQ_URL}/sensors/{sid}/hours", headers=hdr, timeout=60,
                                params={"datetime_from": f"{start}T00:00:00Z",
                                        "datetime_to": f"{end}T23:59:59Z", "limit": 1000,
                                        "page": page})
                if h.status_code != 200:
                    break
                res = h.json().get("results", [])
                for x in res:
                    ts = ((x.get("period") or {}).get("datetimeFrom") or {}).get("utc")
                    if ts is not None and x.get("value") is not None:
                        rows.append({"site_id": p.site_id, "ts_utc": pd.Timestamp(ts).tz_convert("UTC"),
                                     name: float(x["value"])})
                if len(res) < 1000:
                    break
                page += 1
    if not rows:
        return pd.DataFrame(columns=["site_id", "ts_utc", "pm25", "pm10"])
    df = pd.DataFrame(rows)
    return df.groupby(["site_id", "ts_utc"], as_index=False).mean(numeric_only=True)


def fetch_env(points: pd.DataFrame, start: date, end: date, *,
              session: requests.Session | None = None, today: date | None = None) -> pd.DataFrame:
    """Hourly met + PM for ``points`` (site_id, lat, lon), [start, end] UTC dates.

    Requests are chunked by calendar year (Open-Meteo multi-location limits)."""
    from packages.pipelines.synth import env_join
    session = session or _session()
    today = today or datetime.now(timezone.utc).date()
    parts = []
    y0 = start
    while y0 <= end:
        y1 = min(end, date(y0.year, 12, 31))
        met = env_join.fetch_meteo(points, y0, y1, session=session, today_utc=today)
        air = env_join.fetch_air(points, y0, y1, session=session)
        df = met.merge(air, on=["site_id", "ts_utc"], how="left") if not air.empty else met
        parts.append(df)
        y0 = y1 + timedelta(days=1)
    env = pd.concat(parts, ignore_index=True)
    for c in ("pm25", "pm10"):
        if c not in env:
            env[c] = np.nan
    env["pm_source"] = np.where(env["pm25"].notna(), "open-meteo-cams", None)
    key = os.environ.get("OPENAQ_API_KEY")
    missing = env["pm25"].isna() | env["pm10"].isna()
    if missing.any() and key:
        oa = openaq_pm(points, start, end, api_key=key, session=session)
        if not oa.empty:
            env = env.merge(oa.rename(columns={"pm25": "pm25_oa", "pm10": "pm10_oa"}),
                            on=["site_id", "ts_utc"], how="left")
            for c in ("pm25", "pm10"):
                fill = env[c].isna() & env[f"{c}_oa"].notna()
                env.loc[fill, c] = env.loc[fill, f"{c}_oa"]
                env.loc[fill & env["pm_source"].isna(), "pm_source"] = "openaq-v3"
            env = env.drop(columns=["pm25_oa", "pm10_oa"])
    elif missing.any():
        log.info("%d site-hours lack PM; no OPENAQ_API_KEY, left NaN", int(missing.sum()))
    return env.sort_values(["site_id", "ts_utc"]).reset_index(drop=True)


# ---------------------------------------------------------------------------------------
# occupational
# ---------------------------------------------------------------------------------------
def synth_occupational(env: pd.DataFrame, sites: pd.DataFrame, start: date, end: date,
                       seed: int = 42) -> pd.DataFrame:
    """Shift logs against historical weather, one seed per month; -> hourly features."""
    from packages.pipelines.synth.occupational_features import build_feature_frame
    from packages.pipelines.synth.sites import MIX_YAML
    from packages.pipelines.synth.workforce import (daily_site_rollup, generate_shift_log,
                                                    generate_workforce)
    wf = generate_workforce(sites, MIX_YAML, seed=seed)
    logs = []
    for m in months_between(start, end):
        lo = max(start, m.start_time.date())
        hi = min(end, m.end_time.date())
        logs.append(generate_shift_log(wf, lo, hi, env, mix_yaml=MIX_YAML,
                                       seed=_seed(seed, m.strftime("%Y-%m"))))
    shift_log = pd.concat(logs, ignore_index=True)
    roll = daily_site_rollup(shift_log)
    return build_feature_frame(env, roll, wf, shift_log, out_path=None)


# ---------------------------------------------------------------------------------------
# clinical
# ---------------------------------------------------------------------------------------
def synthea_history(start: date, end: date, *, raw_dir: Path = RAW_DIR, population: int = 10000,
                    seed: int | None = None, synthea_dir: Path | None = None
                    ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Events + patients covering [start, end] from one Synthea run (cached as parquet)."""
    from packages.pipelines.synth.synthea_wrapper import parse_fhir_bundles, run_synthea_uae
    raw_dir = Path(raw_dir)
    ev_p, pt_p = raw_dir / "clinical_events.parquet", raw_dir / "clinical_patients.parquet"
    if ev_p.is_file() and pt_p.is_file():
        ev = pd.read_parquet(ev_p)
        t = ev.loc[ev["event_type"] == "encounter", "event_time"]
        if len(t) and t.min() <= pd.Timestamp(start, tz="UTC") and t.max() >= pd.Timestamp(end, tz="UTC"):
            return ev, pd.read_parquet(pt_p)
        log.info("cached Synthea history does not cover %s..%s; regenerating", start, end)
    ref = end + timedelta(days=2)                    # taper lands after the window
    fhir = raw_dir / "clinical" / "fhir"
    if not (fhir.is_dir() and any(fhir.iterdir())):
        seed = seed if seed is not None else _seed("synthea", start, end) % (2 ** 31)
        log.info("running Synthea: population=%d seed=%d reference=%s", population, seed, ref)
        fhir = run_synthea_uae(population, seed, raw_dir / "clinical", synthea_dir=synthea_dir,
                               reference_date=ref, febrile_start_year=start.year)
    window = (ref - start).days + 2
    parsed = raw_dir / "parsed"
    parse_fhir_bundles(fhir, out_dir=parsed, window_days=window, as_of=ref,
                       workers=max(1, (os.cpu_count() or 2) - 1))
    ev, pt = pd.read_parquet(parsed / "events.parquet"), pd.read_parquet(parsed / "patients.parquet")
    raw_dir.mkdir(parents=True, exist_ok=True)
    ev.to_parquet(ev_p, compression="zstd")
    pt.to_parquet(pt_p, compression="zstd")
    return ev, pt


def couple_to_weather(events: pd.DataFrame, patients: pd.DataFrame, station_env: pd.DataFrame,
                      start: date, end: date, *, seed: int = 42) -> pd.DataFrame:
    """Append weather-driven extra heat / respiratory encounters (see module docstring).

    station_env: hourly env at each catchment city (site_id = city name)."""
    from packages.pipelines.fusion.signals import _encounter_flags
    from packages.pipelines.synth.synthea_wrapper import HEAT_SNOMED, RESPIRATORY_SNOMED
    enc = _encounter_flags(events)
    enc = enc.assign(city=enc["patient_id"].map(patients.set_index("patient_id")["city"]))
    t = pd.to_datetime(enc["event_time"], utc=True)
    in_win = (t >= pd.Timestamp(start, tz="UTC")) & (t < pd.Timestamp(end + timedelta(days=1), tz="UTC"))
    n_days = max(1, (end - start).days + 1)
    rate = enc[in_win].groupby("city")[["heat", "resp"]].sum() / n_days   # mean daily rate per city

    e = station_env.copy()
    e["day"] = pd.to_datetime(e["ts_utc"], utc=True).dt.tz_convert("Asia/Dubai").dt.date
    daily = e.groupby(["site_id", "day"]).agg(at_max=("apparent_temp", "max"),
                                              pm10_mean=("pm10", "mean")).reset_index()
    heat_code, resp_code = next(iter(HEAT_SNOMED)), next(iter(RESPIRATORY_SNOMED))
    by_city = patients.groupby("city")["patient_id"].apply(list).to_dict()
    ns = uuid.UUID("2b0a6c1e-5d7f-4e8a-9b3c-1d2e3f405162")
    rows = []
    for d in daily.itertuples():
        if d.day < start or d.day > end or d.site_id not in rate.index or d.site_id not in by_city:
            continue
        heat_mult = min(HEAT_BOOST_CAP, HEAT_BOOST_PER_C * max(0.0, (d.at_max or 0) - HEAT_AT_THRESHOLD)) \
            if pd.notna(d.at_max) else 0.0
        dust_mult = min(DUST_BOOST_CAP, DUST_BOOST_PER_UG * max(0.0, (d.pm10_mean or 0) - DUST_PM10_THRESHOLD)) \
            if pd.notna(d.pm10_mean) else 0.0
        if heat_mult <= 0 and dust_mult <= 0:
            continue
        rng = np.random.default_rng(_seed(seed, d.site_id, d.day))
        day0 = pd.Timestamp(d.day, tz="Asia/Dubai")
        for kind, mult, code in (("heat", heat_mult, heat_code), ("resp", dust_mult, resp_code)):
            n = rng.poisson(rate.at[d.site_id, kind] * mult) if mult > 0 else 0
            for i in range(n):
                pid = by_city[d.site_id][rng.integers(len(by_city[d.site_id]))]
                when = (day0 + pd.Timedelta(hours=float(rng.uniform(8, 22)))).tz_convert("UTC")
                eid = str(uuid.uuid5(ns, f"{d.site_id}|{d.day}|{kind}|{i}"))
                base = {"patient_id": pid, "value": np.nan, "unit": None, "event_time": when,
                        "event_end": when + pd.Timedelta(minutes=45), "encounter_id": eid,
                        "reason_code": code, "encounter_class": "EMER" if kind == "heat" else "AMB"}
                rows.append({**base, "event_type": "encounter", "code": "50849002",
                             "display": "Emergency room admission (weather-coupled synthetic)"})
                rows.append({**base, "event_type": "condition", "code": code,
                             "display": f"weather-coupled synthetic {kind} presentation"})
    if not rows:
        return events
    extra = pd.DataFrame(rows)
    out = pd.concat([events.astype({"event_type": str, "unit": object, "encounter_class": object}),
                     extra], ignore_index=True)
    log.info("weather coupling added %d heat and %d respiratory encounters",
             int((extra["event_type"].eq("encounter") & extra["reason_code"].isin(list(HEAT_SNOMED))).sum()),
             int((extra["event_type"].eq("encounter") & extra["reason_code"].isin(list(RESPIRATORY_SNOMED))).sum()))
    return out


# ---------------------------------------------------------------------------------------
# panel
# ---------------------------------------------------------------------------------------
def history_panel(env: pd.DataFrame, feats: pd.DataFrame, events: pd.DataFrame | None,
                  patients: pd.DataFrame | None, sites: pd.DataFrame, lo: pd.Timestamp,
                  hi: pd.Timestamp, cfg: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The live panel builder over [lo, hi] + provenance columns + the T-2 clinical lag."""
    span = int(np.ceil((hi - lo) / pd.Timedelta(days=1))) + 1
    c = {**cfg, "spatial": {**cfg["spatial"], "history_days": span}}
    panel, units = build_signal_panel(hi, c, sites=sites, env=env, features=feats,
                                      events=events, patients=patients)
    pillar = {s: m["pillar"] for s, m in cfg["signals"].items()}
    panel["pillar"] = panel["signal"].map(pillar).fillna("occ")
    panel["observed_at"] = panel["ts_utc"]
    clin = panel["signal"].isin(CLIN_SIGNALS)
    panel.loc[clin, "ts_utc"] = panel.loc[clin, "ts_utc"] + CLINICAL_LAG
    panel = panel[panel["ts_utc"] <= hi]
    panel["available_at"] = panel["ts_utc"] + PUBLISH_LATENCY
    panel["source"] = np.select(
        [panel["pillar"].eq("env"), panel["pillar"].eq("occ")],
        ["open-meteo-era5+cams", "synthetic-workforce(real-weather)"],
        "synthea+weather-coupling(T-2)")
    return panel[HISTORY_COLUMNS].reset_index(drop=True), units


def catchment_stations(sites: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Clinical catchment cities as weather 'stations' (site_id = city name)."""
    cities = load_cities()
    units = build_units(sites, cfg, cities)
    used = cities[cities["city"].isin(set(units["clinical_city"]))]
    return pd.DataFrame({"site_id": used["city"], "lat": used["lat"], "lon": used["lon"]}).reset_index(drop=True)


def pull_history(start, end, sites: pd.DataFrame | None = None, stations: pd.DataFrame | None = None,
                 *, scenarios_yaml=SCENARIOS_YAML, out_dir: Path = BACKTEST_DIR,
                 raw_dir: Path = RAW_DIR, seed: int = 42, force: bool = False,
                 env: pd.DataFrame | None = None, station_env: pd.DataFrame | None = None,
                 events: pd.DataFrame | None = None, patients: pd.DataFrame | None = None,
                 features: pd.DataFrame | None = None, clinical: bool = True) -> list[Path]:
    """Write data/processed/backtest/{YYYY-MM}.parquet for every month in [start, end].

    sites: occupational sites (default configs/uae_sites.yaml); stations: clinical
    catchment points (default: the catchment cities). ``env`` / ``station_env`` /
    ``events`` / ``patients`` / ``features`` can be injected (tests); otherwise they are fetched or
    generated. Months whose file exists are skipped unless ``force``.
    """
    from packages.pipelines.synth.sites import load_sites
    cfg = load_scenarios(scenarios_yaml)
    out_dir = Path(out_dir)
    months = months_between(start, end)
    todo = [m for m in months if force or not month_path(m, out_dir).is_file()]
    paths = [month_path(m, out_dir) for m in months]
    if not todo:
        log.info("history: all %d months present in %s", len(months), out_dir)
        return paths
    sites = sites if sites is not None else load_sites()
    stations = stations if stations is not None else catchment_stations(sites, cfg)
    d0 = todo[0].start_time.date() - timedelta(days=WARMUP_DAYS)
    d1 = min(todo[-1].end_time.date(), pd.Timestamp(end).date())
    log.info("history: building %s..%s (%d months to write)", d0, d1, len(todo))

    if env is None:
        env = fetch_env(sites[["site_id", "lat", "lon"]], d0 - timedelta(days=1), d1 + timedelta(days=1))
    feats = features if features is not None else synth_occupational(env, sites, d0, d1, seed=seed)
    if clinical:
        if events is None or patients is None:
            events, patients = synthea_history(d0, d1, raw_dir=raw_dir)
        if station_env is None:
            station_env = fetch_env(stations, d0 - timedelta(days=1), d1 + timedelta(days=1))
        events = couple_to_weather(events, patients, station_env, d0, d1, seed=seed)

    lo = pd.Timestamp(d0, tz="UTC")
    hi = pd.Timestamp(d1 + timedelta(days=1), tz="UTC") - pd.Timedelta(hours=1)
    panel, units = history_panel(env, feats, events if clinical else None,
                                 patients if clinical else None, sites, lo, hi, cfg)
    out_dir.mkdir(parents=True, exist_ok=True)
    u = units.copy()
    for c in ("site_ids", "site_names", "emirates", "sectors"):
        u[c] = u[c].map(lambda v: list(v))
    u.to_parquet(out_dir / "units.parquet")
    for m in todo:
        a, b = pd.Timestamp(m.start_time, tz="UTC"), pd.Timestamp(m.end_time, tz="UTC")
        part = panel[(panel["ts_utc"] >= a) & (panel["ts_utc"] <= b)]
        tmp = month_path(m, out_dir).with_suffix(".parquet.tmp")
        part.to_parquet(tmp, compression="zstd")
        os.replace(tmp, month_path(m, out_dir))
        log.info("history: wrote %s (%d rows)", month_path(m, out_dir).name, len(part))
    return paths


def load_history(start, end, out_dir: Path = BACKTEST_DIR) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Concatenate the monthly files covering [start, end] (no filtering by time: the
    replay decides what is visible, and asserts it)."""
    files = [month_path(m, out_dir) for m in months_between(start, end)]
    present = [f.is_file() for f in files]
    if not any(present):
        raise FileNotFoundError(f"no backtest history in {out_dir}; run pull_history first")
    first = present.index(True)
    missing = [f.name for f, ok in zip(files[first:], present[first:]) if not ok]
    if missing:
        raise FileNotFoundError(f"backtest history missing: {missing}; run pull_history first")
    if first:
        log.warning("history starts at %s: warm-up months %s absent; early epochs see less history",
                    files[first].name, [f.name for f in files[:first]])
    files = files[first:]
    panel = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    units = pd.read_parquet(Path(out_dir) / "units.parquet")
    return panel, units
