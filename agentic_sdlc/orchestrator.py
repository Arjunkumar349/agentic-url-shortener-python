"""Dependency-graph orchestration with approval, retry, audit, and rollback."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from uuid import uuid4


Handler = Callable[[dict[str, Any]], dict[str, Any]]
Approval = Callable[[str], bool]
SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|password|secret|token)\s*[:=]\s*['\"]?"
    r"(?!\[REDACTED\])[A-Za-z0-9_./+=-]{8,}"
)


@dataclass(frozen=True)
class TaskSpec:
    id: str
    depends_on: tuple[str, ...]
    handler: str | None = None
    max_attempts: int = 2
    fallback_handler: str | None = None
    approval_required: bool = False


@dataclass
class WorkflowState:
    run_id: str
    scenario: str
    task_status: dict[str, str]
    task_outputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    attempts: dict[str, int] = field(default_factory=dict)
    artifacts: dict[str, list[str]] = field(default_factory=dict)
    audit: list[dict[str, Any]] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    status: str = "running"
    started_at: str = field(default_factory=lambda: _now())
    finished_at: str | None = None

    @classmethod
    def create(cls, scenario: str, context: dict[str, Any], tasks: list[TaskSpec]) -> WorkflowState:
        return cls(
            run_id=uuid4().hex[:12],
            scenario=scenario,
            task_status={task.id: "pending" for task in tasks},
            context=context,
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> WorkflowState:
        return cls(**value)


class PolicyViolation(ValueError):
    """Raised when generated artifacts violate a local safety rule."""


class WorkflowEngine:
    def __init__(
        self,
        tasks: list[TaskSpec],
        handlers: dict[str, Handler],
        run_dir: Path,
        max_workers: int = 4,
    ) -> None:
        self.tasks = {task.id: task for task in tasks}
        self.handlers = handlers
        self.run_dir = run_dir.resolve()
        self.max_workers = max(1, max_workers)
        self._audit_lock = threading.Lock()
        self._validate_graph()

    def run(self, state: WorkflowState, approve: Approval) -> WorkflowState:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._save(state)

        while True:
            for task in self.tasks.values():
                if state.task_status[task.id] != "waiting_approval":
                    continue
                if approve(task.id):
                    state.task_status[task.id] = "succeeded"
                    self._emit(state, "approval_granted", task.id)
                    self._save(state)
                else:
                    return self._pause(state, task.id)

            ready = [
                task for task in self.tasks.values()
                if state.task_status[task.id] == "pending"
                and all(state.task_status[parent] == "succeeded" for parent in task.depends_on)
            ]
            if not ready:
                pending = [task for task in self.tasks.values() if state.task_status[task.id] == "pending"]
                if pending:
                    self._fail(state, f"No runnable tasks; unresolved dependencies for {[t.id for t in pending]}")
                else:
                    state.status = "completed"
                    state.finished_at = _now()
                    self._emit(state, "workflow_completed")
                    self._save(state)
                return state

            executable: list[TaskSpec] = []
            for task in ready:
                if task.approval_required:
                    state.task_status[task.id] = "waiting_approval"
                    self._emit(state, "approval_requested", task.id)
                    if approve(task.id):
                        state.task_status[task.id] = "succeeded"
                        self._emit(state, "approval_granted", task.id)
                    else:
                        return self._pause(state, task.id)
                else:
                    state.task_status[task.id] = "running"
                    executable.append(task)
            self._save(state)

            if not executable:
                continue

            snapshot = {
                **state.context,
                "outputs": json.loads(json.dumps(state.task_outputs)),
                "artifacts": json.loads(json.dumps(state.artifacts)),
                "run_dir": str(self.run_dir),
                "workflow": {
                    "started_at": state.started_at,
                    "task_status": dict(state.task_status),
                    "attempts": dict(state.attempts),
                    "audit": json.loads(json.dumps(state.audit)),
                },
            }
            results: dict[str, tuple[dict[str, Any] | None, int, bool, str | None, float | None]] = {}
            with ThreadPoolExecutor(max_workers=min(self.max_workers, len(executable))) as pool:
                futures = {
                    pool.submit(self._execute, task, snapshot): task.id
                    for task in executable
                }
                for future in as_completed(futures):
                    results[futures[future]] = future.result()

            for task in executable:
                result, attempts, used_fallback, error, recovery_seconds = results[task.id]
                state.attempts[task.id] = attempts
                if result is None:
                    state.task_status[task.id] = "failed"
                    self._emit(state, "task_failed", task.id, {"error": error})
                    self._fail(state, f"Task {task.id} failed: {error}")
                    return state
                try:
                    if not isinstance(result, dict):
                        raise TypeError("task handler must return a mapping")
                    artifact_map = result.pop("artifacts", {})
                    written = self._write_artifacts(task.id, artifact_map)
                except (PolicyViolation, OSError, TypeError) as exc:
                    state.task_status[task.id] = "failed"
                    self._emit(state, "policy_or_artifact_failure", task.id, {"error": str(exc)})
                    self._fail(state, f"Task {task.id} artifact rejected: {exc}")
                    return state
                state.task_outputs[task.id] = result
                state.artifacts[task.id] = written
                state.task_status[task.id] = "succeeded"
                self._emit(state, "task_succeeded", task.id, {
                    "attempts": attempts,
                    "fallback_used": used_fallback,
                    "recovery_seconds": recovery_seconds,
                    "artifact_count": len(written),
                })
            self._save(state)

    def replan(self, state: WorkflowState, upstream_task: str, revised_output: dict[str, Any]) -> list[str]:
        """Replace one completed upstream result and invalidate its downstream dependents."""
        if upstream_task not in self.tasks or state.task_status[upstream_task] != "succeeded":
            raise ValueError(f"Task {upstream_task!r} must be a succeeded task in this workflow")
        descendants = self._descendants(upstream_task)
        state.task_outputs[upstream_task] = revised_output
        for task_id in descendants:
            state.task_status[task_id] = "pending"
            state.task_outputs.pop(task_id, None)
            state.attempts.pop(task_id, None)
            for relative in state.artifacts.pop(task_id, []):
                path = (self.run_dir / relative).resolve()
                if path.is_relative_to(self.run_dir) and path.is_file():
                    path.unlink()
        state.status = "running"
        state.finished_at = None
        self._emit(state, "workflow_replanned", upstream_task, {
            "invalidated_tasks": descendants,
            "revised_output_sha256": _digest(revised_output),
        })
        self._save(state)
        return descendants

    def _execute(
        self, task: TaskSpec, context: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, int, bool, str | None, float | None]:
        handler_name = task.handler
        if handler_name is None:
            return {}, 0, False, None, None
        last_error: str | None = None
        attempts = max(1, task.max_attempts)
        started = datetime.now(timezone.utc)
        for attempt in range(1, attempts + 1):
            try:
                result = self.handlers[handler_name](context)
                recovery = (datetime.now(timezone.utc) - started).total_seconds() if attempt > 1 else None
                return result, attempt, False, None, recovery
            except Exception as exc:  # Agent/tool failures are bounded and recorded.
                last_error = f"{type(exc).__name__}: {exc}"
        if task.fallback_handler:
            try:
                result = self.handlers[task.fallback_handler](context)
                recovery = (datetime.now(timezone.utc) - started).total_seconds()
                return result, attempts, True, None, recovery
            except Exception as exc:
                last_error = f"fallback {type(exc).__name__}: {exc}"
        return None, attempts, False, last_error, None

    def _write_artifacts(self, task_id: str, artifacts: Any) -> list[str]:
        if not isinstance(artifacts, dict):
            raise TypeError("artifacts must be a path-to-text mapping")
        validated: list[tuple[Path, Path, str]] = []
        for name, content in artifacts.items():
            relative = PurePosixPath(name)
            if relative.is_absolute() or not relative.parts or any(part in ("..", "") for part in relative.parts):
                raise PolicyViolation(f"unsafe artifact path: {name}")
            if not isinstance(content, str):
                raise TypeError(f"artifact {name!r} must contain text")
            if SECRET_PATTERN.search(content):
                raise PolicyViolation(f"possible credential in artifact {name!r}")
            destination = (self.run_dir / "artifacts" / task_id / Path(*relative.parts)).resolve()
            artifact_root = (self.run_dir / "artifacts" / task_id).resolve()
            if not destination.is_relative_to(artifact_root):
                raise PolicyViolation(f"artifact escaped output directory: {name}")
            if destination.exists():
                raise PolicyViolation(f"refusing to overwrite existing artifact: {name}")
            validated.append((destination, artifact_root, content))

        created: list[Path] = []
        try:
            for destination, _, content in validated:
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_name(destination.name + ".tmp")
                try:
                    temporary.write_text(content, encoding="utf-8")
                    temporary.replace(destination)
                    created.append(destination)
                finally:
                    temporary.unlink(missing_ok=True)
        except OSError:
            for path in created:
                path.unlink(missing_ok=True)
            raise
        return [path.relative_to(self.run_dir).as_posix() for path in created]

    def _pause(self, state: WorkflowState, task_id: str) -> WorkflowState:
        state.status = "paused_for_approval"
        self._emit(state, "workflow_paused", task_id)
        self._save(state)
        return state

    def _fail(self, state: WorkflowState, reason: str) -> None:
        state.status = "failed"
        state.finished_at = _now()
        self._emit(state, "safe_stop", details={"reason": reason})
        removed = 0
        artifact_root = self.run_dir / "artifacts"
        if artifact_root.exists():
            for path in artifact_root.rglob("*"):
                if path.is_file():
                    path.unlink()
                    removed += 1
        for task_id, paths in list(state.artifacts.items()):
            state.artifacts.pop(task_id, None)
            if state.task_status.get(task_id) == "succeeded":
                state.task_status[task_id] = "rolled_back"
                state.task_outputs.pop(task_id, None)
        for task_id, status in state.task_status.items():
            if status in ("pending", "running"):
                state.task_status[task_id] = "cancelled"
        self._emit(state, "rollback_completed", details={"artifacts_removed": removed})
        self._save(state)

    def _validate_graph(self) -> None:
        if not self.tasks:
            raise ValueError("workflow must contain at least one task")
        for task in self.tasks.values():
            missing = set(task.depends_on) - self.tasks.keys()
            if missing:
                raise ValueError(f"task {task.id!r} has unknown dependencies: {sorted(missing)}")
            if task.handler and task.handler not in self.handlers:
                raise ValueError(f"missing handler {task.handler!r} for task {task.id!r}")
            if task.fallback_handler and task.fallback_handler not in self.handlers:
                raise ValueError(f"missing fallback {task.fallback_handler!r} for task {task.id!r}")
        visited: set[str] = set()
        active: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in active:
                raise ValueError(f"workflow contains a cycle at {task_id!r}")
            if task_id in visited:
                return
            active.add(task_id)
            for parent in self.tasks[task_id].depends_on:
                visit(parent)
            active.remove(task_id)
            visited.add(task_id)

        for task_id in self.tasks:
            visit(task_id)

    def _descendants(self, task_id: str) -> list[str]:
        descendants: set[str] = set()
        changed = True
        while changed:
            changed = False
            for task in self.tasks.values():
                if task.id not in descendants and (task_id in task.depends_on or descendants.intersection(task.depends_on)):
                    descendants.add(task.id)
                    changed = True
        return [task.id for task in self.tasks.values() if task.id in descendants]

    def _emit(
        self,
        state: WorkflowState,
        event: str,
        task_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        entry = {"at": _now(), "event": event, "task": task_id, "details": details or {}}
        with self._audit_lock:
            state.audit.append(entry)
            with (self.run_dir / "audit.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, sort_keys=True) + "\n")

    def _save(self, state: WorkflowState) -> None:
        destination = self.run_dir / "state.json"
        temporary = destination.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(state.__dict__, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(destination)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()