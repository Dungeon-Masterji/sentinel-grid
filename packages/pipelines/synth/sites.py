"""Site roster for the occupational pillar (configs/uae_sites.yaml).

    python -m packages.pipelines.synth.sites --check     # verify h3_res8 matches lat/lon

Site names and coordinates are real (OpenStreetMap). Worker counts, shift patterns
and heat exposure are modelled, and every worker is synthetic (docs/ETHICS.md).
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[3]
SITES_YAML = REPO_ROOT / "configs" / "uae_sites.yaml"
MIX_YAML = REPO_ROOT / "configs" / "workforce_mix.yaml"

SECTORS = {"construction", "logistics", "industrial", "ports", "aviation"}
SHIFT_PATTERNS = {"double_12h", "triple_8h", "day_only"}
HEAT_EXPOSURES = {"outdoor", "mixed", "indoor_hot", "indoor_cool"}
SITE_COLUMNS = ["site_id", "name", "operator", "sector", "emirate", "lat", "lon", "h3_res8",
                "worker_count", "shift_pattern", "heat_exposure"]


def load_yaml(path: Path | str) -> dict:
    import yaml
    with open(path) as f:
        return yaml.safe_load(f)


def load_sites(sites_yaml: Path | str | pd.DataFrame = SITES_YAML) -> pd.DataFrame:
    """Return the site roster as a DataFrame (one row per site, validated)."""
    if isinstance(sites_yaml, pd.DataFrame):
        return sites_yaml
    raw = load_yaml(sites_yaml).get("sites") or []
    df = pd.DataFrame(raw).rename(columns={"id": "site_id"})
    missing = set(SITE_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"{sites_yaml}: sites missing fields {sorted(missing)}")
    df = df[SITE_COLUMNS].copy()
    problems = []
    if df["site_id"].duplicated().any():
        problems.append("duplicate site ids")
    for col, allowed in (("sector", SECTORS), ("shift_pattern", SHIFT_PATTERNS),
                         ("heat_exposure", HEAT_EXPOSURES)):
        bad = sorted(set(df[col]) - allowed)
        if bad:
            problems.append(f"{col} has unknown values {bad}")
    if not df["worker_count"].between(200, 2000).all():
        problems.append("worker_count must be within 200–2000")
    if not (df["lat"].between(22, 27).all() and df["lon"].between(51, 57).all()):
        problems.append("lat/lon outside the UAE bounding box")
    if problems:
        raise ValueError(f"{sites_yaml}: " + "; ".join(problems))
    df["worker_count"] = df["worker_count"].astype(int)
    return df.reset_index(drop=True)


def h3_mismatches(sites: pd.DataFrame) -> list[tuple[str, str, str]]:
    """(site_id, stored, computed) for every site whose h3_res8 is stale."""
    import h3
    out = []
    for r in sites.itertuples():
        cell = h3.latlng_to_cell(r.lat, r.lon, 8)
        if cell != r.h3_res8:
            out.append((r.site_id, r.h3_res8, cell))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sites", type=Path, default=SITES_YAML)
    ap.add_argument("--check", action="store_true", help="verify h3_res8 against lat/lon")
    args = ap.parse_args(argv)
    sites = load_sites(args.sites)
    print(sites[["site_id", "sector", "worker_count", "shift_pattern", "heat_exposure",
                 "h3_res8"]].to_string(index=False))
    print(f"\n{len(sites)} sites, {sites['worker_count'].sum():,} workers")
    if args.check:
        bad = h3_mismatches(sites)
        for sid, stored, cell in bad:
            print(f"STALE h3_res8 for {sid}: yaml={stored} computed={cell}")
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
