"""Spatial aggregation onto the common H3 fusion grid (resolution 7 by default).

* aggregate_to_h3()      point records -> per-hex, per-timestep aggregates, with
                         small-cell suppression applied HERE (privacy/suppress.py)
* nearest_station_join() target hex -> nearest environmental station hex
* catchment_map()        site hex -> nearest qualifying city hex (clinical catchment)
* hex_boundary_geojson() GeoJSON Polygon for a cell

How each pillar lands on the grid:
  clinical      patient home city centroid (configs/synthea/.../uae_cities.csv)
  environmental nearest station (the Open-Meteo/CAMS point at each site)
  occupational  site location (configs/uae_sites.yaml)
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from packages.pipelines.privacy.suppress import DEFAULT_MIN_RECORDS, suppress_small_cells

AGGS = ("mean", "sum", "count")


def latlng_to_h3(lat, lon, res: int = 7) -> np.ndarray:
    import h3
    return np.array([h3.latlng_to_cell(float(a), float(b), res) if pd.notna(a) and pd.notna(b)
                     else None for a, b in zip(lat, lon)], dtype=object)


def aggregate_to_h3(signal_df: pd.DataFrame, lat_col: str, lon_col: str, time_col: str,
                    value_col: str | None, *, res: int = 7, freq: str = "1h",
                    agg: str = "mean", weight_col: str | None = None,
                    window: str | None = None, start=None, end=None,
                    min_records: int = DEFAULT_MIN_RECORDS) -> pd.DataFrame:
    """Aggregate point records to (h3, ts_utc) cells.

    Parameters
    ----------
    value_col   column to aggregate (ignored for agg="count")
    agg         "mean" (weighted by weight_col if given), "sum", or "count"
    weight_col  records each row represents (e.g. workers behind a site-hour row);
                default 1 per row. Contributing records = sum of weights.
    freq        time bucket (rows are floored to it; ts is the bucket start, UTC)
    window      optional trailing window (e.g. "24h"): each output step aggregates
                records in (t - window, t + freq). The grid is filled between start/end,
                and buckets with no records count as zero records.
    min_records small-cell threshold. Cells with fewer contributing records have value
                and n_records masked to NaN (suppressed=True). Use min_records=1 only
                for non-personal data (environmental readings).

    Returns columns: h3, ts_utc, value, n_records, suppressed.
    """
    if agg not in AGGS:
        raise ValueError(f"agg must be one of {AGGS}")
    df = signal_df
    cols = ["h3", "ts_utc", "value", "n_records", "suppressed"]
    if df.empty:
        return pd.DataFrame(columns=cols)
    ts = pd.to_datetime(df[time_col], utc=True).dt.floor(freq)
    w = df[weight_col].astype(float).fillna(0.0) if weight_col else pd.Series(1.0, index=df.index)
    v = (pd.Series(1.0, index=df.index) if agg == "count"
         else df[value_col].astype(float))
    work = pd.DataFrame({"h3": latlng_to_h3(df[lat_col], df[lon_col], res), "ts_utc": ts,
                         "w": w.to_numpy(), "v": v.to_numpy()})
    work = work[work["h3"].notna() & work["v"].notna()]
    work["wv"] = work["w"] * work["v"]
    g = work.groupby(["h3", "ts_utc"], sort=True).agg(sw=("w", "sum"), swv=("wv", "sum"),
                                                     n=("v", "size"))

    if window is not None:
        lo = pd.Timestamp(start).tz_convert("UTC").floor(freq) if start is not None else g.index.get_level_values(1).min()
        hi = pd.Timestamp(end).tz_convert("UTC").floor(freq) if end is not None else g.index.get_level_values(1).max()
        grid = pd.date_range(lo, hi, freq=freq)
        parts = []
        for cell, sub in g.groupby(level=0):
            s = sub.droplevel(0).reindex(grid, fill_value=0.0)
            r = s.rolling(window, min_periods=1).sum()
            r.index.name = "ts_utc"
            r["h3"] = cell
            parts.append(r.reset_index())
        g = (pd.concat(parts, ignore_index=True).set_index(["h3", "ts_utc"])
             if parts else g.iloc[0:0])
    elif start is not None or end is not None:
        t = g.index.get_level_values(1)
        keep = np.ones(len(g), dtype=bool)
        if start is not None:
            keep &= t >= pd.Timestamp(start).tz_convert("UTC")
        if end is not None:
            keep &= t <= pd.Timestamp(end).tz_convert("UTC")
        g = g[keep]

    out = g.reset_index()
    if agg == "mean":
        out["value"] = np.where(out["sw"] > 0, out["swv"] / out["sw"].replace(0, np.nan), np.nan)
    elif agg == "sum":
        out["value"] = out["swv"]
    else:
        out["value"] = out["sw"]
    out["n_records"] = out["sw"] if weight_col else out["n"]
    out = suppress_small_cells(out, count_col="n_records", value_cols=("value",),
                               min_records=min_records)
    return out[cols].reset_index(drop=True)


def _haversine_km(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = (np.sin((lat2 - lat1) * p / 2) ** 2
         + np.cos(lat1 * p) * np.cos(lat2 * p) * np.sin((lon2 - lon1) * p / 2) ** 2)
    return 12742 * np.arcsin(np.sqrt(a))


def _centroid(cell: str) -> tuple[float, float]:
    import h3
    return h3.cell_to_latlng(cell)


def nearest_station_join(target_hexes, stations: pd.DataFrame) -> pd.DataFrame:
    """Map each target hex to its nearest station. ``stations`` needs station_id, lat,
    lon. Returns target_h3, station_id, station_h3, distance_km."""
    rows = []
    slat, slon = stations["lat"].to_numpy(float), stations["lon"].to_numpy(float)
    res = None
    for cell in target_hexes:
        import h3
        res = res or h3.get_resolution(cell)
        lat, lon = _centroid(cell)
        d = _haversine_km(lat, lon, slat, slon)
        i = int(np.argmin(d))
        rows.append({"target_h3": cell, "station_id": stations["station_id"].iloc[i],
                     "station_h3": latlng_to_h3([slat[i]], [slon[i]], res)[0],
                     "distance_km": round(float(d[i]), 2)})
    return pd.DataFrame(rows, columns=["target_h3", "station_id", "station_h3", "distance_km"])


def catchment_map(target_hexes, cities: pd.DataFrame, *, res: int = 7,
                  min_population: int = 0) -> pd.DataFrame:
    """Map each target hex to the nearest city with population >= min_population.
    ``cities`` needs city, lat, lon, population. Returns target_h3, city, city_h3,
    distance_km."""
    c = cities[cities["population"] >= min_population].reset_index(drop=True)
    if c.empty:
        raise ValueError("no city meets min_population")
    stations = c.rename(columns={"city": "station_id"})
    j = nearest_station_join(target_hexes, stations)
    j = j.rename(columns={"station_id": "city", "station_h3": "city_h3"})
    j["city_h3"] = latlng_to_h3(c.set_index("city").loc[j["city"], "lat"],
                                c.set_index("city").loc[j["city"], "lon"], res)
    return j


def hex_boundary_geojson(cell: str) -> dict:
    """GeoJSON Polygon (lon, lat order, closed ring) for an H3 cell."""
    import h3
    ring = [[round(lng, 6), round(lat, 6)] for lat, lng in h3.cell_to_boundary(cell)]
    ring.append(ring[0])
    return {"type": "Polygon", "coordinates": [ring]}
