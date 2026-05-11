#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from codex_agent.config import ensure_directories, settings
from codex_agent.graph import build_graph


def make_run_id() -> str:
    return datetime.now().strftime("run_%Y%m%d_%H%M%S")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the AURA LangGraph retargeting agent.")
    parser.add_argument("--source", required=True, help="Path to the source Spectre .scs netlist.")
    parser.add_argument("--target-specs", required=True, help="CSV of target performance specs.")
    parser.add_argument("--target-pdk", default="ptm22_lp", help="Target PDK preset, e.g. ptm22_lp, gpdk045, asap7.")
    parser.add_argument("--prompt", default="", help="User migration prompt.")
    parser.add_argument("--run-id", default=None, help="Optional run id.")
    parser.add_argument("--max-iterations", type=int, default=None, help="Override optimization iterations.")
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    ensure_directories()
    args = parse_args()

    run_id = args.run_id or make_run_id()
    run_dir = settings.runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    initial_state: dict[str, Any] = {
        "run_id": run_id,
        "run_dir": str(run_dir),
        "source_scs_path": str(Path(args.source).expanduser().resolve()),
        "target_specs_csv_path": str(Path(args.target_specs).expanduser().resolve()),
        "target_pdk": args.target_pdk,
        "user_prompt": args.prompt,
        "iteration": 0,
        "retarget_attempt": 0,
        "compile_debug_attempt": 0,
        "max_iterations": args.max_iterations if args.max_iterations is not None else settings.max_iterations,
        "artifacts": {},
        "events": [],
    }

    graph = build_graph()
    final_state = graph.invoke(initial_state)

    state_path = run_dir / "final_state.json"
    state_path.write_text(json.dumps(final_state, indent=2, default=str), encoding="utf-8")

    print(f"Run ID: {run_id}")
    print(f"Run dir: {run_dir}")
    print(f"Report: {final_state.get('migration_report_path', '')}")
    print(f"Final netlist: {final_state.get('final_scs_path', '')}")
    print(f"Metrics CSV: {final_state.get('measured_csv_path', '')}")
    print(f"Specs met: {final_state.get('specs_met', False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

