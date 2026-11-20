"""Deterministic scenario agents used to exercise the orchestration engine."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

from .orchestrator import TaskSpec


SCENARIOS = {
    "greenfield": {
        "requirement": (
            "Build a Python URL shortener with an API to create short links, redirect to the "
            "original URL, and report per-link click analytics. Use persistent storage, validate "
            "inputs, and handle short-code collisions safely."
        ),
        "ambiguities": [],
    },
    "brownfield": {
        "requirement": "Extend an existing URL shortener with analytics without breaking current behavior.",
        "ambiguities": ["The existing public API and analytics compatibility requirements are not specified."],
    },
    "ambiguous": {
        "requirement": "Improve the URL shortener's analytics and reliability.",
        "ambiguities": [
            "Required analytics dimensions, retention, and privacy expectations are unknown.",
            "Traffic volume, availability target, and acceptable redirect latency are unspecified.",
            "Authentication, link expiration, and deployment constraints are not defined.",
        ],
    },
}


TASKS = [
    TaskSpec("understand", (), handler="understand"),
    TaskSpec("decompose", ("understand",), handler="decompose"),
    TaskSpec("design", ("decompose",), handler="design"),
    TaskSpec("design_approval", ("design",), approval_required=True),
    TaskSpec("implement", ("design_approval",), handler="implement", max_attempts=2, fallback_handler="implementation_fallback"),
    TaskSpec("unit_validation", ("implement",), handler="unit_validation"),
    TaskSpec("documentation", ("implement",), handler="documentation"),
    TaskSpec("integration_validation", ("unit_validation", "documentation"), handler="integration_validation"),
    TaskSpec("release_approval", ("integration_validation",), approval_required=True),
    TaskSpec("release_readiness", ("release_approval",), handler="release_readiness"),
]


def build_scenario(name: str, workspace: Path) -> tuple[list[TaskSpec], dict[str, Any], dict[str, Any]]:
    if name not in SCENARIOS:
        raise ValueError(f"unknown scenario {name!r}")
    source_files = _inventory(workspace) if name == "brownfield" else []
    context = {
        "scenario": name,
        "requirement": SCENARIOS[name]["requirement"],
        "ambiguities": SCENARIOS[name]["ambiguities"],
        "workspace": str(workspace.resolve()),
        "source_inventory": source_files,
    }

    def understand(ctx: dict[str, Any]) -> dict[str, Any]:
        return {
            "normalized_problem": ctx["requirement"],
            "ambiguities": ctx["ambiguities"],
            "clarification_required": bool(ctx["ambiguities"]),
            "evidence": ctx["source_inventory"],
        }

    def decompose(ctx: dict[str, Any]) -> dict[str, Any]:
        return {"work_items": [
            {"id": "contract", "depends_on": [], "deliverable": "Define create, redirect, and analytics API behavior"},
            {"id": "storage", "depends_on": ["contract"], "deliverable": "Persist short links and click counters reliably"},
            {"id": "implementation", "depends_on": ["storage"], "deliverable": "Implement validated APIs and collision-safe codes"},
            {"id": "tests", "depends_on": ["implementation"], "deliverable": "Cover API behavior, analytics, and failure cases"},
            {"id": "documentation", "depends_on": ["implementation"], "deliverable": "Document APIs, assumptions, setup, and limits"},
            {"id": "release", "depends_on": ["tests", "documentation"], "deliverable": "Review readiness and risks"},
        ]}

    def design(ctx: dict[str, Any]) -> dict[str, Any]:
        decisions = [
            "Keep generated changes in a run-scoped artifact directory; never edit the target workspace automatically.",
            "Run test and documentation work as independent branches, then synchronize before release review.",
            "Use explicit human approval before implementation and release readiness.",
        ]
        if ctx["scenario"] == "greenfield":
            decisions.extend([
                "Expose POST /api/urls to create a short link, GET /{code} to redirect, and GET /api/urls/{code}/stats for click totals.",
                "Use SQLite for durable local persistence, atomic click updates, and uniqueness-enforced short codes.",
                "Validate destination URLs as HTTP(S); defer authentication, expiration, and distributed deployment because the brief does not define them.",
            ])
        if ctx["scenario"] == "brownfield":
            decisions.append("Use a bounded file inventory as evidence; file impact suggestions are heuristic, not semantic analysis.")
        if ctx["scenario"] == "greenfield":
            risks = [
                "The public stats endpoint exposes click totals and destination URLs; add access control if those are sensitive.",
                "SQLite is appropriate for a single-instance prototype, not a horizontally scaled production deployment.",
                "Authentication, expiration, analytics retention, and availability targets remain undefined.",
            ]
        else:
            risks = ctx["ambiguities"] or ["Generated proposal requires owner review before adoption."]
        if ctx["ambiguities"]:
            decisions.append("Record unresolved product decisions as assumptions and block claims of production readiness.")
        return {"decisions": decisions, "risks": risks}

    def implement(ctx: dict[str, Any]) -> dict[str, Any]:
        scenario = ctx["scenario"]
        if scenario == "ambiguous":
            return {"change_mode": "blocked_pending_clarification", "artifacts": {
                "requirements-clarification.md": (
                    "# Requirements Clarification\n\n"
                    "Implementation is intentionally withheld until the owner answers these questions:\n\n"
                    + "\n".join(f"- {item}" for item in ctx["ambiguities"])
                    + "\n\nNo code or deployment changes are proposed. Re-run after updating the normalized requirement."
                ),
                "pending-decisions.json": json.dumps({"status": "blocked", "questions": ctx["ambiguities"]}, indent=2),
            }}
        if scenario == "brownfield":
            inventory = ctx["source_inventory"]
            related = [path for path in inventory if any(word in path.lower() for word in ("url", "short", "redirect", "analytics", "link"))]
            targets = related or inventory[:10] or ["(no source files discovered)"]
            body = "\n".join(f"- `{path}`" for path in targets)
            return {"change_mode": "proposal_only", "artifacts": {
                "change-plan.md": (
                    "# Brownfield Change Proposal\n\n"
                    "No source files were changed. Candidate review targets from the bounded workspace inventory:\n\n"
                    f"{body}\n\n"
                    "Confirm ownership, callers, and compatibility behavior before implementation."
                ),
                "contract.json": json.dumps({"operation": "URL shortener analytics", "compatibility": "preserve existing routes and response formats; confirm before change"}, indent=2),
            }}
        project_root = Path(__file__).resolve().parent.parent
        proposal_files = {
            "url_shortener/__init__.py": project_root / "url_shortener" / "__init__.py",
            "url_shortener/__main__.py": project_root / "url_shortener" / "__main__.py",
            "url_shortener/service.py": project_root / "url_shortener" / "service.py",
            "tests/test_url_shortener.py": project_root / "tests" / "test_url_shortener.py",
            "schemas/url-shortener.schema.json": project_root / "schemas" / "url-shortener.schema.json",
        }
        return {"change_mode": "proposal_only", "artifacts": {
            name: path.read_text(encoding="utf-8")
            for name, path in proposal_files.items()
        }}

    def implementation_fallback(ctx: dict[str, Any]) -> dict[str, Any]:
        return {"change_mode": "fallback_plan", "artifacts": {
            "implementation-fallback.md": "# Implementation Fallback\n\nAutomated generation failed. No source changes were made; use the accepted design and decomposition for manual implementation."
        }}

    def unit_validation(ctx: dict[str, Any]) -> dict[str, Any]:
        artifact_root = Path(ctx["run_dir"]) / "artifacts" / "implement"
        checked: list[str] = []
        for path in artifact_root.rglob("*.py"):
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            checked.append(path.relative_to(artifact_root).as_posix())
        for path in artifact_root.rglob("*.json"):
            json.loads(path.read_text(encoding="utf-8"))
            checked.append(path.relative_to(artifact_root).as_posix())
        if not checked:
            raise ValueError("implementation produced no statically checkable artifacts")
        return {"result": "passed", "checks": checked, "execution": "static-only; generated code is not executed"}

    def documentation(ctx: dict[str, Any]) -> dict[str, Any]:
        unresolved = "\n".join(f"- {item}" for item in ctx["ambiguities"]) or "- None identified for this scenario."
        return {"artifacts": {
            "README.md": (
                f"# {ctx['scenario'].title()} Engineering Proposal\n\n"
                f"## Requirement\n{ctx['requirement']}\n\n"
                "## API proposal\n"
                "- `POST /api/urls` creates a short link from an HTTP(S) destination.\n"
                "- `GET /{code}` redirects and records a click.\n"
                "- `GET /api/urls/{code}/stats` returns creation time, click count, and last access time.\n\n"
                "## Reliability assumptions\n"
                "SQLite provides durable local storage; database transactions keep link creation and click counts consistent. "
                "Authentication, expiration, retention, and production availability targets require product decisions.\n\n"
                f"## Assumptions and open questions\n{unresolved}\n\n"
                "## Validation\nGenerated Python is syntax-checked, JSON is parsed, and integration checks verify required deliverables.\n"
                "Generated code is not executed automatically. Review and run it in an isolated project environment.\n"
            )
        }}

    def integration_validation(ctx: dict[str, Any]) -> dict[str, Any]:
        outputs = ctx["outputs"]
        required = {"understand", "decompose", "design", "implement", "unit_validation", "documentation"}
        missing = sorted(required - outputs.keys())
        if missing:
            raise ValueError(f"missing stage outputs: {missing}")
        if outputs["unit_validation"].get("result") != "passed":
            raise ValueError("unit validation did not pass")
        implementation = outputs["implement"]
        if ctx["scenario"] == "greenfield":
            required_artifacts = {
                "url_shortener/service.py",
                "tests/test_url_shortener.py",
                "schemas/url-shortener.schema.json",
            }
            produced = set(ctx["artifacts"].get("implement", []))
            missing_artifacts = sorted(
                f"artifacts/implement/{name}" for name in required_artifacts
                if f"artifacts/implement/{name}" not in produced
            )
            if missing_artifacts or implementation.get("change_mode") != "proposal_only":
                raise ValueError(f"greenfield implementation deliverables missing or invalid: {missing_artifacts}")
        return {
            "result": "passed",
            "synchronized_branches": ["unit_validation", "documentation"],
            "all_required_stages_present": True,
        }

    def release_readiness(ctx: dict[str, Any]) -> dict[str, Any]:
        outputs = ctx["outputs"]
        workflow = ctx["workflow"]
        statuses = workflow["task_status"]
        completed = sum(status == "succeeded" for status in statuses.values())
        concluded = sum(status in ("succeeded", "failed", "rolled_back") for status in statuses.values())
        attempts = list(workflow["attempts"].values())
        recoveries = [
            event["details"]["recovery_seconds"]
            for event in workflow["audit"]
            if event["event"] == "task_succeeded" and event["details"].get("recovery_seconds") is not None
        ]
        started = __import__("datetime").datetime.fromisoformat(workflow["started_at"])
        elapsed = max(0.0, (__import__("datetime").datetime.now(started.tzinfo) - started).total_seconds())
        rollback_count = sum(event["event"] == "rollback_completed" for event in workflow["audit"])
        metrics = {
            "completed_task_success_rate": completed / concluded if concluded else None,
            "retry_count": sum(max(0, count - 1) for count in attempts),
            "retry_frequency": sum(max(0, count - 1) for count in attempts) / len(attempts) if attempts else 0.0,
            "rollback_count": rollback_count,
            "rollback_frequency": 1.0 if rollback_count else 0.0,
            "mttr_seconds": sum(recoveries) / len(recoveries) if recoveries else None,
            "end_to_end_latency_seconds_at_report": elapsed,
        }
        summary = {
            "scenario": ctx["scenario"],
            "requirement": ctx["requirement"],
            "decisions": outputs["design"]["decisions"],
            "risks": outputs["design"]["risks"],
            "validation": outputs["integration_validation"],
            "limitations": ["Deterministic fixture agents; no LLM or external tools are invoked.", "Generated artifacts require human review; no automatic workspace mutation."],
            "release_status": "review_ready_not_deployed",
            "metrics": metrics,
        }
        if ctx["ambiguities"]:
            summary["release_status"] = "blocked_pending_clarification"
        return {"summary": summary, "artifacts": {
            "engineering-summary.json": json.dumps(summary, indent=2),
            "engineering-summary.md": (
                f"# Engineering Summary: {ctx['scenario'].title()}\n\n"
                f"**Status:** {summary['release_status']}\n\n"
                f"**Requirement:** {ctx['requirement']}\n\n"
                "## Decisions\n" + "\n".join(f"- {item}" for item in summary["decisions"])
                + "\n\n## Risks and open questions\n" + "\n".join(f"- {item}" for item in summary["risks"])
                + "\n\n## Validation\nStatic artifact checks and synchronized-stage checks passed. Generated code was not executed.\n"
                + "\n## Run metrics\n```json\n" + json.dumps(metrics, indent=2) + "\n```\n"
            ),
        }}

    return TASKS, {
        "understand": understand,
        "decompose": decompose,
        "design": design,
        "implement": implement,
        "implementation_fallback": implementation_fallback,
        "unit_validation": unit_validation,
        "documentation": documentation,
        "integration_validation": integration_validation,
        "release_readiness": release_readiness,
    }, context


def _inventory(workspace: Path) -> list[str]:
    ignored = {".git", ".venv", "venv", "node_modules", "__pycache__", ".agentic-runs"}
    files = []
    for path in workspace.rglob("*"):
        if not path.is_file() or any(part in ignored for part in path.relative_to(workspace).parts):
            continue
        files.append(path.relative_to(workspace).as_posix())
        if len(files) >= 500:
            break
    return sorted(files)