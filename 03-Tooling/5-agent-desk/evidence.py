#!/usr/bin/env python3
"""Objective, clustered paper-evidence reporting for copy-desk promotion."""
from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from config import CONFIG


def _cluster_key(row: dict) -> str:
    evidence = row.get("evidence") or {}
    return str(evidence.get("source_wallet") or row.get("token") or "unknown")


def summarize_outcomes(rows: Iterable[dict], *, bootstrap_iterations: int = 2000,
                       seed: int = 4663) -> dict:
    rows = list(rows)
    returns = [float(row.get("estimated_net_pnl_eth", 0) or 0) for row in rows]
    clusters = defaultdict(list)
    for row, value in zip(rows, returns):
        clusters[_cluster_key(row)].append(value)
    cluster_values = list(clusters.values())
    means = []
    if cluster_values:
        rng = random.Random(seed)
        for _ in range(max(1, int(bootstrap_iterations))):
            sample = [rng.choice(cluster_values) for _ in range(len(cluster_values))]
            flattened = [value for cluster in sample for value in cluster]
            means.append(sum(flattened) / len(flattened))
    means.sort()
    lower_index = max(0, int(len(means) * 0.05) - 1) if means else 0
    lower_bound = means[lower_index] if means else 0.0
    wins = [value for value in returns if value > 0]
    losses = [value for value in returns if value <= 0]
    return {
        "observations": len(rows),
        "independent_clusters": len(clusters),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(rows) if rows else 0.0,
        "net_expectancy_eth": sum(returns) / len(returns) if returns else 0.0,
        "expectancy_lower_bound_eth": lower_bound,
        "total_net_pnl_eth": sum(returns),
        "sellability_verified_observations": sum(bool(row.get("sellability_verified")) for row in rows),
        "unsellable_outcomes": sum(str(row.get("outcome", "")).startswith("unsellable") for row in rows),
    }


def promotion_readiness(summary: dict, *, min_observations: int = 75) -> dict:
    reasons = []
    observations = int(summary.get("observations", 0))
    if observations < int(min_observations):
        reasons.append("insufficient_observations")
    if float(summary.get("expectancy_lower_bound_eth", 0) or 0) <= 0:
        reasons.append("nonpositive_clustered_lower_bound")
    if int(summary.get("sellability_verified_observations", 0)) < observations:
        reasons.append("sellability_not_verified")
    if int(summary.get("unsellable_outcomes", 0)) > 0:
        reasons.append("unsellable_outcomes_present")
    return {"ready": not reasons, "reasons": reasons}


def build_report(campaign_path: Path = None, log_path: Path = None) -> dict:
    campaign_path = campaign_path or (Path(CONFIG.trade_log_path).parent / "evidence_campaign.json")
    log_path = log_path or Path(CONFIG.trade_log_path)
    campaign = json.loads(campaign_path.read_text()) if campaign_path.exists() else {"start_ts": 0}
    start_ts = float(campaign.get("start_ts", 0) or 0)
    events = []
    if log_path.exists():
        for line in log_path.read_text().splitlines():
            try:
                row = json.loads(line)
            except (TypeError, ValueError):
                continue
            if float(row.get("ts", 0) or 0) >= start_ts:
                events.append(row)
    outcomes = [row for row in events if row.get("event") == "paper_exit"]
    summary = summarize_outcomes(outcomes)
    readiness = promotion_readiness(summary, min_observations=75)
    return {
        "campaign": campaign,
        "events_since_start": len(events),
        "pipeline_candidates": sum(row.get("event") == "pipeline_complete" for row in events),
        "route_preflight_failures": sum(row.get("event") == "executor_gate" and row.get("outcome") == "preflight_failed" for row in events),
        "paper_entries": sum(row.get("event") == "executor_execute" and row.get("outcome") == "paper_opened" for row in events),
        "paper_exits": len(outcomes),
        "summary": summary,
        "promotion": readiness,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    report = build_report()
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
