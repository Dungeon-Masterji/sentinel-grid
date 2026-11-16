"""Load + validate configs/scenarios.yaml."""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCENARIOS_YAML = REPO_ROOT / "configs" / "scenarios.yaml"
PILLARS = {"env", "occ", "clin"}
SEVERITIES = ("watch", "advisory", "warning")


def load_scenarios(path: Path | str | dict = SCENARIOS_YAML) -> dict:
    if isinstance(path, dict):
        cfg = path
    else:
        import yaml
        with open(path) as f:
            cfg = yaml.safe_load(f)
    sig = cfg.get("signals") or {}
    problems = []
    for name, s in sig.items():
        if s.get("pillar") not in PILLARS:
            problems.append(f"signal {name}: pillar must be one of {sorted(PILLARS)}")
    ids = set()
    for sc in cfg.get("scenarios") or []:
        sid = sc.get("id")
        if sid in ids:
            problems.append(f"duplicate scenario id {sid}")
        ids.add(sid)
        for s in sc.get("signals", []):
            if s["name"] not in sig:
                problems.append(f"{sid}: unknown signal {s['name']}")
        w = sum(s["weight"] for s in sc.get("signals", []))
        if abs(w - 1.0) > 1e-6:
            problems.append(f"{sid}: weights sum to {w}, expected 1.0")
        th = sc.get("thresholds") or {}
        if [th.get(k) for k in SEVERITIES] != sorted(th.get(k, 0) for k in SEVERITIES):
            problems.append(f"{sid}: thresholds must be watch <= advisory <= warning")
        for k in SEVERITIES:
            if k not in (sc.get("recommended_actions") or {}):
                problems.append(f"{sid}: recommended_actions missing {k}")
    if problems:
        raise ValueError("scenarios.yaml: " + "; ".join(problems))
    return cfg


def unique_signals(cfg: dict) -> list[str]:
    """Signals used by at least one scenario, in first-use order (no duplicates)."""
    seen: list[str] = []
    for sc in cfg["scenarios"]:
        for s in sc["signals"]:
            if s["name"] not in seen:
                seen.append(s["name"])
    return seen
