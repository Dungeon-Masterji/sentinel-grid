"""Bayesian fusion of per-signal anomaly scores into P(scenario firing at t).

Model (per scenario, pooled across hexes and timesteps, i.e. hierarchical):

    sigma            ~ HalfNormal(3)                      shared disagreement scale (the pool)
    p[i]             ~ Beta(2, 2)                         P(scenario firing) at row i = (hex, t)
    logit(x[i, k])   ~ Normal(logit(p[i]), sigma / sqrt(w_k * K))
                                                          observed score of signal k (0..1)

So the posterior centre is a weight-averaged log-odds of the signal scores (a
logarithmic opinion pool), shrunk towards the Beta(2,2) prior. sigma is learned from how
much the signals disagree across all hexes and hours, and sets the width of the
credible interval. Missing / suppressed signals are simply absent from the likelihood,
so the posterior widens instead of being imputed. Scores are clipped to [0.01, 0.99]
before the logit, so a gated 0 is strong but finite "not anomalous" evidence.

Two modes:
* mode="map"      (default; fast enough for Azure F1 and the 15-min cron)
                  sigma: MAP of its marginal posterior via scipy.optimize (p integrated
                  out numerically). p[i]: exact 1-D posterior on a fine grid, from which the
                  mean and equal-tailed 90% credible interval come.
* mode="sampled"  the same model in PyMC, sampled with NUTS. Use it offline or in a
                  backtest when the full joint posterior is needed.

Drivers: contributions() gives each signal's share w_k * x_k / sum_j(w_j * x_j) (%), stored
on every alert so the fused score stays explainable.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.optimize import minimize_scalar
from scipy.special import betaln, logit, logsumexp

EPS = 0.01
SIGMA_PRIOR_SD = 3.0                # HalfNormal scale on sigma (log-odds units)


def _prep(scores: pd.DataFrame, weights: dict[str, float]):
    names = [k for k in weights if k in scores.columns]
    X = scores[names].to_numpy(dtype=float)
    M = np.isfinite(X)
    X = np.clip(np.where(M, X, 0.5), EPS, 1 - EPS)
    w = np.array([weights[k] for k in names], dtype=float)
    return names, X, M, w


def _log_post_grid(X, M, w, sigma, grid, prior):
    """(rows, grid) unnormalised log posterior of p."""
    K = len(w)
    a0, b0 = prior
    lp = ((a0 - 1) * np.log(grid) + (b0 - 1) * np.log1p(-grid) - betaln(a0, b0))[None, :]
    lp = np.repeat(lp, X.shape[0], axis=0)
    L, mu = logit(X), logit(grid)[None, :]
    for k in range(X.shape[1]):
        sd = sigma / np.sqrt(w[k] * K)
        term = -0.5 * ((L[:, k][:, None] - mu) / sd) ** 2 - np.log(sd) - 0.5 * np.log(2 * np.pi)
        lp += np.where(M[:, k][:, None], term, 0.0)
    return lp


def fit_sigma_map(X, M, w, *, prior=(2, 2), grid_n: int = 199, max_rows: int = 3000,
                  seed: int = 42) -> float:
    """MAP of sigma under its marginal posterior (p integrated out on a grid)."""
    rows = np.where(M.any(axis=1))[0]
    if rows.size == 0:
        return SIGMA_PRIOR_SD
    if rows.size > max_rows:
        rows = np.random.default_rng(seed).choice(rows, size=max_rows, replace=False)
    Xs, Ms = X[rows], M[rows]
    grid = np.linspace(0.5 / grid_n, 1 - 0.5 / grid_n, grid_n)
    dlog = np.log(1.0 / grid_n)

    def neg(logs):
        sg = float(np.exp(logs))
        lml = logsumexp(_log_post_grid(Xs, Ms, w, sg, grid, prior), axis=1) + dlog
        lprior = -0.5 * (sg / SIGMA_PRIOR_SD) ** 2 + logs     # HalfNormal + log-Jacobian
        return -(lml.sum() + lprior)

    r = minimize_scalar(neg, bounds=(np.log(0.05), np.log(20.0)), method="bounded",
                        options={"xatol": 1e-3})
    return float(np.exp(r.x))


def _grid_posterior(X, M, w, sigma, prior, grid_n, ci):
    grid = np.linspace(0.5 / grid_n, 1 - 0.5 / grid_n, grid_n)
    lp = _log_post_grid(X, M, w, sigma, grid, prior)
    post = np.exp(lp - lp.max(axis=1, keepdims=True))
    post /= post.sum(axis=1, keepdims=True)
    mean = post @ grid
    cdf = np.cumsum(post, axis=1)
    lo_q, hi_q = (1 - ci) / 2, 1 - (1 - ci) / 2
    lo = grid[np.clip((cdf < lo_q).sum(axis=1), 0, grid_n - 1)]
    hi = grid[np.clip((cdf < hi_q).sum(axis=1), 0, grid_n - 1)]
    return mean, lo, hi


def fuse(scores: pd.DataFrame, weights: dict[str, float], *, mode: str = "map",
         prior=(2, 2), sigma: float | None = None, ci: float = 0.9, grid_n: int = 199,
         rows: np.ndarray | None = None, seed: int = 42, sample_kwargs: dict | None = None
         ) -> pd.DataFrame:
    """Fuse per-signal scores (one column per signal, NaN = missing) for one scenario.

    Returns a frame aligned to ``scores`` (or to ``scores.iloc[rows]``) with columns
    mean, ci_low, ci_high, coverage (observed share of scenario weight), sigma.
    sigma is fitted on all rows of ``scores`` unless given.
    """
    if mode not in ("map", "sampled"):
        raise ValueError("mode must be 'map' or 'sampled'")
    names, X, M, w = _prep(scores, weights)
    w_all = np.array(list(weights.values()), dtype=float)
    coverage = (M * w).sum(axis=1) / w_all.sum()
    sel = np.arange(len(scores)) if rows is None else np.asarray(rows)

    if mode == "map":
        sg = sigma if sigma is not None else fit_sigma_map(X, M, w, prior=prior, seed=seed)
        mean, lo, hi = _grid_posterior(X[sel], M[sel], w, sg, prior, grid_n, ci)
        sig = np.full(sel.size, sg)
    else:
        mean, lo, hi, sig = _fuse_pymc(X, M, w, sel, prior=prior, ci=ci, seed=seed,
                                       **(sample_kwargs or {}))
    idx = scores.index[sel]
    out = pd.DataFrame({"mean": mean, "ci_low": lo, "ci_high": hi,
                        "coverage": coverage[sel], "sigma": sig}, index=idx)
    none = ~M[sel].any(axis=1)
    out.loc[none, ["mean", "ci_low", "ci_high"]] = np.nan
    return out


def _fuse_pymc(X, M, w, sel, *, prior, ci, seed, draws=500, tune=500, chains=2,
               history_rows=300):
    """Full NUTS posterior for the selected rows (plus a history subsample to inform sigma)."""
    import pymc as pm
    rng = np.random.default_rng(seed)
    others = np.setdiff1d(np.where(M.any(axis=1))[0], sel)
    hist = rng.choice(others, size=min(history_rows, others.size), replace=False) if others.size else others
    use = np.concatenate([sel, hist]).astype(int)
    Lu, Mu = logit(X[use]), M[use]
    K = len(w)
    with pm.Model():
        sigma = pm.HalfNormal("sigma", sigma=SIGMA_PRIOR_SD)
        p = pm.Beta("p", alpha=prior[0], beta=prior[1], shape=len(use))
        mu = pm.math.logit(p)
        for k in range(K):
            r = np.where(Mu[:, k])[0]
            if r.size == 0:
                continue
            pm.Normal(f"x{k}", mu=mu[r], sigma=sigma / np.sqrt(w[k] * K), observed=Lu[r, k])
        idata = pm.sample(draws=draws, tune=tune, chains=chains, random_seed=seed,
                          progressbar=False, compute_convergence_checks=False)
    ps = idata.posterior["p"].stack(s=("chain", "draw")).values[: len(sel)]   # (n_sel, S)
    ss = idata.posterior["sigma"].values.ravel()
    q = [(1 - ci) / 2, 1 - (1 - ci) / 2]
    return (ps.mean(axis=1), np.quantile(ps, q[0], axis=1), np.quantile(ps, q[1], axis=1),
            np.full(len(sel), float(ss.mean())))


def contributions(scores_row: dict[str, float], weights: dict[str, float]) -> dict[str, float]:
    """Each available signal's share (percent) of sum_k(w_k * score_k); sums to 100."""
    parts = {k: weights[k] * float(v) for k, v in scores_row.items()
             if k in weights and v is not None and np.isfinite(v)}
    tot = sum(parts.values())
    if tot <= 0:
        n = len(parts) or 1
        return {k: round(100.0 / n, 1) for k in parts}
    return {k: round(100.0 * v / tot, 1) for k, v in parts.items()}
