#!/usr/bin/env python3
"""Bounded paper-only evidence campaign runner."""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

from config import CONFIG
from desk import desk_cycle
from evidence import build_report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycles", type=int, default=100)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--report", type=Path,
                        default=Path(CONFIG.trade_log_path).parent / "evidence_report.json")
    parser.add_argument("--logfile", type=Path, default=None,
                        help="Redirect stdout to this log file (avoids BrokenPipeError)")
    args = parser.parse_args()
    if args.logfile is not None:
        fh = open(str(args.logfile), "a")
        sys.stdout = fh
    state_path = Path(CONFIG.trade_log_path).parent / "evidence_runner_state.json"
    for index in range(max(0, args.cycles)):
        started = time.time()
        try:
            cycle = desk_cycle(paper_mode=True)
            error = None
        except Exception:
            cycle = None
            error = traceback.format_exc(limit=5)
        report = build_report()
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        state_path.write_text(json.dumps({
            "paper_only": True,
            "cycle_index": index + 1,
            "cycles_requested": args.cycles,
            "last_cycle_started_ts": started,
            "last_cycle_result": cycle,
            "last_error": error,
            "report_path": str(args.report),
        }, indent=2, sort_keys=True) + "\n")
        if index + 1 < args.cycles:
            time.sleep(max(1, args.interval))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
