"""Command-line interface for running and resuming governed workflows."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .orchestrator import WorkflowEngine, WorkflowState
from .scenarios import SCENARIOS, build_scenario


def _approver(requested: set[str]):
    def approve(task_id: str) -> bool:
        checkpoint = "design" if task_id == "design_approval" else "release"
        if checkpoint in requested or "all" in requested:
            return True
        print(f"Approval required for {checkpoint}. Re-run with --approve {checkpoint} to continue.")
        return False
    return approve


def _run_dir(output: Path, run_id: str) -> Path:
    return output / run_id


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentic-sdlc", description="Run a governed SDLC prototype scenario.")
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run", help="start a scenario")
    run_parser.add_argument("--scenario", choices=sorted(SCENARIOS), required=True)
    run_parser.add_argument("--workspace", type=Path, default=Path.cwd())
    run_parser.add_argument("--output", type=Path, default=Path(".agentic-runs"))
    run_parser.add_argument("--approve", action="append", choices=("design", "release", "all"), default=[])
    resume_parser = commands.add_parser("resume", help="resume a paused workflow")
    resume_parser.add_argument("run_id")
    resume_parser.add_argument("--output", type=Path, default=Path(".agentic-runs"))
    resume_parser.add_argument("--approve", action="append", choices=("design", "release", "all"), default=[])
    args = parser.parse_args(argv)

    output = args.output.resolve()
    if args.command == "run":
        tasks, handlers, context = build_scenario(args.scenario, args.workspace)
        state = WorkflowState.create(args.scenario, context, tasks)
    else:
        state_path = _run_dir(output, args.run_id) / "state.json"
        if not state_path.is_file():
            parser.error(f"no saved workflow state at {state_path}")
        state = WorkflowState.from_dict(json.loads(state_path.read_text(encoding="utf-8")))
        tasks, handlers, _ = build_scenario(state.scenario, Path(state.context["workspace"]))

    run_dir = _run_dir(output, state.run_id)
    engine = WorkflowEngine(tasks, handlers, run_dir)
    result = engine.run(state, _approver(set(args.approve)))
    print(json.dumps({
        "run_id": result.run_id,
        "scenario": result.scenario,
        "status": result.status,
        "run_directory": str(run_dir),
        "task_status": result.task_status,
    }, indent=2))
    return 0 if result.status == "completed" else (2 if result.status == "paused_for_approval" else 1)


if __name__ == "__main__":
    raise SystemExit(main())