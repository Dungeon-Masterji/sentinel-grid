"""Build the long signal panel the detectors consume, from processed pillar data.

    panel: signal, h3, ts_utc, value, n_records, suppressed     (hourly, UTC, <= as_of)
    units: one row per fusion unit (a site hex) with where each pillar is read from

Sources (all filtered to ts <= as_of_time; nothing reads the clock):
  env   data/processed/env/site_hourly.parquet      Open-Meteo / CAMS at each site point
        ("stations"); a unit reads its nearest station.
  occ   data/processed/features/occupational.parquet site-hour absence, weighted by
        scheduled workers; suppressed below spatial.min_records workers.
  clin  data/processed/clinical/{events,patients}.parquet. Encounters are placed at the
        patient's HOME CITY centroid (uae_cities.csv) and counted over a trailing 24 h,
        suppressed below min_records encounters. A unit reads the clinical signal of
        its catchment city (nearest city with population >= clinical_catchment_min_population).
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from packages.pipelines.fusion.config import load_scenarios
from packages.pipelines.fusion.spatial import (aggregate_to_h3, catchment_map, latlng_to_h3,
                                               nearest_station_join)

log = logging.getLogger("sentinel.fusion.signals")
REPO_ROOT = Path(__file__).resolve().parents[3]
PROCESSED = REPO_ROOT / "data" / "processed"
CITIES_CSV = REPO_ROOT / "configs" / "synthea" / "resources" / "geography" / "uae_cities.csv"

ENV_COLUMNS = {"apparent_temp": "apparent_temp", "pm25": "pm25", "pm10": "pm10"}
OCC_COLUMNS = {"absence_rate": "absence_rate_last_1h",
               "heat_absence_rate": "heat_absence_rate_last_1h",
               "respiratory_absence_rate": "respiratory_absence_rate_last_1h"}
CLIN_SIGNALS = ("respiratory_encounter_count", "heat_related_encounter_count",
                "any_encounter_count", "fever_encounter_count")
PANEL_COLUMNS = ["signal", "h3", "ts_utc", "value", "n_records", "suppressed"]


def load_cities(path: Path = CITIES_CSV) -> pd.DataFrame:
    c = pd.read_csv(path)
    return pd.DataFrame({"city": c["CITY"], "lat": c["LATITUDE"].astype(float),
                         "lon": c["LONGITUDE"].astype(float),
                         "population": c["POPULATION"].astype(int)})


def build_units(sites: pd.DataFrame, cfg: dict, cities: pd.DataFrame | None = None) -> pd.DataFrame:
    """Fusion units = H3 cells containing at least one site."""
    sp = cfg["spatial"]
    res = int(sp["h3_res"])
    s = sites.copy()
    s["h3"] = latlng_to_h3(s["lat"], s["lon"], res)
    units = (s.groupby("h3", sort=True)
              .agg(site_ids=("site_id", list), site_names=("name", list),
                   emirates=("emirate", lambda x: sorted(set(x))),
                   sectors=("sector", lambda x: sorted(set(x))),
                   roster=("worker_count", "sum")).reset_index())
    stations = pd.DataFrame({"station_id": s["site_id"], "lat": s["lat"], "lon": s["lon"]})
    env = nearest_station_join(units["h3"], stations)
    units["env_station"] = env["station_id"].to_numpy()
    units["env_h3"] = env["station_h3"].to_numpy()
    units["env_distance_km"] = env["distance_km"].to_numpy()
    cities = cities if cities is not None else load_cities()
    cat = catchment_map(units["h3"], cities, res=res,
                        min_population=int(sp.get("clinical_catchment_min_population", 0)))
    units["clinical_city"] = cat["city"].to_numpy()
    units["clinical_h3"] = cat["city_h3"].to_numpy()
    units["clinical_distance_km"] = cat["distance_km"].to_numpy()
    return units


def _encounter_flags(events: pd.DataFrame) -> pd.DataFrame:
    from packages.pipelines.synth.synthea_wrapper import (FEBRILE_SNOMED, HEAT_SNOMED,
                                                          RESPIRATORY_SNOMED, _encounters_with_dx)
    enc = events[events["event_type"] == "encounter"][["patient_id", "encounter_id", "event_time"]]
    enc = enc.drop_duplicates("encounter_id")
    resp = _encounters_with_dx(events, set(RESPIRATORY_SNOMED))
    heat = _encounters_with_dx(events, set(HEAT_SNOMED))
    fever = _encounters_with_dx(events, set(FEBRILE_SNOMED))
    enc = enc.assign(resp=enc["encounter_id"].isin(resp).astype(float),
                     heat=enc["encounter_id"].isin(heat).astype(float),
                     fever=enc["encounter_id"].isin(fever).astype(float))
    return enc


def build_signal_panel(as_of_time, scenarios_yaml=None, *, processed_dir: Path = PROCESSED,
                       sites: pd.DataFrame | None = None, env: pd.DataFrame | None = None,
                       features: pd.DataFrame | None = None, events: pd.DataFrame | None = None,
                       patients: pd.DataFrame | None = None,
                       cities: pd.DataFrame | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (panel, units) for history_days up to ``as_of_time`` (inclusive)."""
    cfg = load_scenarios(scenarios_yaml) if scenarios_yaml is not None else load_scenarios()
    sp = cfg["spatial"]
    res, min_rec = int(sp["h3_res"]), int(sp["min_records"])
    as_of = pd.Timestamp(as_of_time).tz_convert("UTC")
    end = as_of.floor("h")
    start = end - pd.Timedelta(days=int(sp.get("history_days", 90)))

    if sites is None:
        from packages.pipelines.synth.sites import load_sites
        sites = load_sites()
    cities = cities if cities is not None else load_cities()
    units = build_units(sites, cfg, cities)
    loc = sites.set_index("site_id")[["lat", "lon"]]
    parts = []

    def _window(df, col):
        t = pd.to_datetime(df[col], utc=True)
        return df[(t >= start) & (t <= as_of)]

    # --- environmental: station = the Open-Meteo/CAMS point at each site ------------------
    env = env if env is not None else pd.read_parquet(processed_dir / "env" / "site_hourly.parquet")
    env = _window(env, "ts_utc").join(loc, on="site_id")
    for sig, col in ENV_COLUMNS.items():
        a = aggregate_to_h3(env, "lat", "lon", "ts_utc", col, res=res, agg="mean",
                            min_records=1)          # non-personal readings: no suppression
        parts.append(a.assign(signal=sig))

    # --- occupational: weighted by scheduled workers; suppressed below min_records ----------
    feats = features if features is not None else pd.read_parquet(
        processed_dir / "features" / "occupational.parquet")
    feats = _window(feats, "ts_utc").join(loc, on="site_id")
    feats = feats[feats["scheduled_on_site"] > 0]
    for sig, col in OCC_COLUMNS.items():
        if col not in feats.columns:
            log.warning("features missing %s; %s skipped (re-run occupational_run)", col, sig)
            continue
        a = aggregate_to_h3(feats, "lat", "lon", "ts_utc", col, res=res, agg="mean",
                            weight_col="scheduled_on_site", min_records=min_rec)
        parts.append(a.assign(signal=sig))

    # --- clinical: home-city centroid, trailing 24 h counts; suppressed below min_records ---
    clin_dir = processed_dir / "clinical"
    if events is None and (clin_dir / "events.parquet").is_file():
        events = pd.read_parquet(clin_dir / "events.parquet")
        patients = pd.read_parquet(clin_dir / "patients.parquet")
    if events is not None and patients is not None:
        enc = _encounter_flags(events)
        t = pd.to_datetime(enc["event_time"], utc=True)
        enc = enc[(t > start - pd.Timedelta(hours=24)) & (t <= as_of)]
        city = patients.set_index("patient_id")["city"]
        enc = enc.assign(city=enc["patient_id"].map(city))
        enc = enc.join(cities.set_index("city")[["lat", "lon"]], on="city")
        kw = dict(res=res, window="24h", start=start, end=end, min_records=min_rec)
        for sig, col, agg in (("respiratory_encounter_count", "resp", "sum"),
                              ("heat_related_encounter_count", "heat", "sum"),
                              ("fever_encounter_count", "fever", "sum"),
                              ("any_encounter_count", None, "count")):
            a = aggregate_to_h3(enc, "lat", "lon", "event_time", col, agg=agg, **kw)
            parts.append(a.assign(signal=sig))
    else:
        log.warning("no clinical data found in %s; clinical signals absent", clin_dir)

    panel = pd.concat(parts, ignore_index=True)[PANEL_COLUMNS]
    panel = panel[(panel["ts_utc"] >= start) & (panel["ts_utc"] <= as_of)].reset_index(drop=True)
    return panel, units
