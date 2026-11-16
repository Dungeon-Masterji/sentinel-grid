"""Small-cell suppression.

Hard privacy rule for Sentinel Grid: an aggregate built from fewer than
``min_records`` contributing person-level records (patient encounters, workers) is
masked. Its value AND its record count become NaN, so a downstream consumer cannot
recover "3 encounters in this hex". Applied inside
packages/pipelines/fusion/spatial.aggregate_to_h3, never left to callers.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

DEFAULT_MIN_RECORDS = 5


def suppress_small_cells(df: pd.DataFrame, *, count_col: str = "n_records",
                         value_cols: list[str] | tuple[str, ...] = ("value",),
                         min_records: int = DEFAULT_MIN_RECORDS) -> pd.DataFrame:
    """Mask ``value_cols`` and ``count_col`` where ``count_col`` < ``min_records``.

    Adds a boolean ``suppressed`` column. Returns a new frame.
    """
    if min_records < 1:
        raise ValueError("min_records must be >= 1")
    out = df.copy()
    small = out[count_col].fillna(0) < min_records
    for c in value_cols:
        out[c] = out[c].astype("float64").where(~small, np.nan)
    out[count_col] = out[count_col].astype("float64").where(~small, np.nan)
    out["suppressed"] = small.to_numpy()
    return out
