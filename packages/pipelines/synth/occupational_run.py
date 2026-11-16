"""Occupational pillar pipeline: env pull -> synthetic workforce -> shift log -> rollup -> features.

    python -m packages.pipelines.synth.occupational_run                 # last 90 days to today (Asia/Dubai)
    python -m packages.pipelines.synth.occupational_run --skip-env      # reuse site_hourly.parquet
    python -m packages.pipelines.synth.occupational_run --api-dir apps/api/data/occupational

Outputs (all synthetic, timestamps UTC):
    data/processed/env/site_hourly.parquet
    data/processed/occupational/{sites,workforce,shift_log,daily_site_rollup}.parquet
    data/processed/features/occupational.parquet

--api-dir copies the three small files the API serves (sites, daily_site_rollup,
features) into a deploy directory. Used by .github/workflows/main_sentinel-grid-api.yml.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import pandas as pd  # noqa: E402

from packages.pipelines.synth import env_join  # noqa: E402
from packages.pipelines.synth.occupational_features import (  # noqa: E402
    FEATURES_PATH, OCC_DIR, build_feature_frame, write_parquet)
from packages.pipelines.synth.sites import MIX_YAML, SITES_YAML, load_sites  # noqa: E402
from packages.pipelines.synth.workforce import (  # noqa: E402
    SYNTHETIC_NOTICE, daily_site_rollup, generate_shift_log, generate_workforce)

DUBAI = ZoneInfo("Asia/Dubai")
API_FILES = {"sites.parquet": OCC_DIR / "sites.parquet",
             "daily_site_rollup.parquet": OCC_DIR / "daily_site_rollup.parquet",
             "features.parquet": FEATURES_PATH}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", type=date.fromisoformat, default=None, help="Asia/Dubai date")
    ap.add_argument("--end", type=date.fromisoformat, default=None, help="Asia/Dubai date")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--skip-env", action="store_true", help="reuse existing site_hourly.parquet")
    ap.add_argument("--api-dir", type=Path, default=None)
    args = ap.parse_args(argv)

    print(SYNTHETIC_NOTICE)
    end = args.end or datetime.now(DUBAI).date()
    start = args.start or end - timedelta(days=args.days - 1)
    t0 = time.time()
    sites = load_sites(SITES_YAML)

    if args.skip_env:
        print(f"[1/5] env: reusing {env_join.OUT_PATH}")
    else:
        print(f"[1/5] env: {start} → {end} (Asia/Dubai)")
        env_join.pull_env_for_sites(sites, start, end)
    env = pd.read_parquet(env_join.OUT_PATH)
    lo = pd.Timestamp(start, tz=DUBAI).tz_convert("UTC")
    hi = pd.Timestamp(end + timedelta(days=2), tz=DUBAI).tz_convert("UTC")   # night shifts spill over
    env = env[(env["ts_utc"] >= lo) & (env["ts_utc"] < hi)]
    t1 = time.time()

    print(f"[2/5] workforce: seed={args.seed}")
    wf = generate_workforce(sites, MIX_YAML, seed=args.seed)
    print(f"[3/5] shift log: {start} → {end}")
    log = generate_shift_log(wf, start, end, env, mix_yaml=MIX_YAML, seed=args.seed)
    print("[4/5] daily rollup")
    roll = daily_site_rollup(log)
    print("[5/5] features")
    feats = build_feature_frame(env, roll, wf, log, out_path=FEATURES_PATH)
    t2 = time.time()

    sites_out = sites.copy()
    sites_out["roster_size"] = sites_out["site_id"].map(wf.groupby("site_id", observed=True).size())
    write_parquet(sites_out, OCC_DIR / "sites.parquet")
    write_parquet(wf, OCC_DIR / "workforce.parquet")
    write_parquet(log, OCC_DIR / "shift_log.parquet")
    write_parquet(roll, OCC_DIR / "daily_site_rollup.parquet")

    print(f"\nworkers {len(wf):,} · shifts {len(log):,} · feature rows {len(feats):,} · "
          f"env {t1 - t0:.0f}s, synth {t2 - t1:.0f}s")
    print("absence reasons:", log["absence_reason"].value_counts().to_dict())
    month = pd.to_datetime(roll["date"]).dt.to_period("M")
    print("\nMean daily rates by month (all sites):")
    print(roll.groupby(month)[["absence_rate", "heat_absence_rate", "respiratory_absence_rate",
                               "mean_ppe_compliance"]].mean().round(4).to_string())
    last = roll[roll["date"] == roll["date"].max()]
    print(f"\nLatest rota day {roll['date'].max()}:")
    print(last[["site_id", "scheduled_count", "absence_rate", "mean_ppe_compliance",
                "peak_absence_hour"]].to_string(index=False))

    if args.api_dir:
        args.api_dir.mkdir(parents=True, exist_ok=True)
        for name, src in API_FILES.items():
            shutil.copy2(src, args.api_dir / name)
        print(f"\ncopied API bundle to {args.api_dir}: {', '.join(API_FILES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
