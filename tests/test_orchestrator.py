import json
import tempfile
import threading
import unittest
from pathlib import Path

from agentic_sdlc.orchestrator import TaskSpec, WorkflowEngine, WorkflowState
from agentic_sdlc.scenarios import build_scenario


class WorkflowEngineTests(unittest.TestCase):
    def test_approval_pause_resume_parallel_sync_and_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            tasks, handlers, context = build_scenario("greenfield", Path(directory))
            engine = WorkflowEngine(tasks, handlers, Path(directory) / "run")
            state = WorkflowState.create("greenfield", context, tasks)

            paused = engine.run(state, lambda task_id: False)
            self.assertEqual(paused.status, "paused_for_approval")
            self.assertEqual(paused.task_status["design_approval"], "waiting_approval")
            saved = json.loads((Path(directory) / "run" / "state.json").read_text())
            resumed = WorkflowState.from_dict(saved)
            done = engine.run(resumed, lambda task_id: True)

            self.assertEqual(done.status, "completed")
            self.assertEqual(done.task_status["release_readiness"], "succeeded")
            self.assertTrue((Path(directory) / "run" / "artifacts" / "release_readiness" / "engineering-summary.md").is_file())
            implementation = Path(directory) / "run" / "artifacts" / "implement"
            service = (implementation / "url_shortener" / "service.py").read_text(encoding="utf-8")
            self.assertIn("def record_click", service)
            self.assertTrue((implementation / "tests" / "test_url_shortener.py").is_file())
            self.assertTrue((implementation / "schemas" / "url-shortener.schema.json").is_file())
            self.assertTrue(any(event["event"] == "workflow_completed" for event in done.audit))

    def test_parallel_siblings_overlap(self):
        barrier = threading.Barrier(2)
        tasks = [
            TaskSpec("start", (), handler="start"),
            TaskSpec("left", ("start",), handler="branch"),
            TaskSpec("right", ("start",), handler="branch"),
            TaskSpec("join", ("left", "right"), handler="join"),
        ]

        def branch(_context):
            barrier.wait(timeout=2)
            return {"ok": True}

        handlers = {"start": lambda _context: {}, "branch": branch, "join": lambda _context: {"joined": True}}
        with tempfile.TemporaryDirectory() as directory:
            engine = WorkflowEngine(tasks, handlers, Path(directory))
            state = WorkflowState.create("parallel", {}, tasks)
            result = engine.run(state, lambda _task: True)
            self.assertEqual(result.status, "completed")

    def test_retries_then_fallback_and_policy_safe_stop(self):
        calls = {"primary": 0}

        def primary(_context):
            calls["primary"] += 1
            raise RuntimeError("simulated tool outage")

        tasks = [TaskSpec("work", (), handler="primary", max_attempts=2, fallback_handler="fallback")]
        handlers = {"primary": primary, "fallback": lambda _context: {"artifacts": {"fallback.md": "manual route"}}}
        with tempfile.TemporaryDirectory() as directory:
            engine = WorkflowEngine(tasks, handlers, Path(directory))
            state = WorkflowState.create("fallback", {}, tasks)
            result = engine.run(state, lambda _task: True)
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.attempts["work"], 2)
            self.assertTrue((Path(directory) / "artifacts" / "work" / "fallback.md").is_file())

        unsafe = {"work": lambda _context: {"artifacts": {"../../escape.txt": "blocked"}}}
        with tempfile.TemporaryDirectory() as directory:
            engine = WorkflowEngine([TaskSpec("work", (), handler="work")], unsafe, Path(directory))
            state = WorkflowState.create("policy", {}, [TaskSpec("work", (), handler="work")])
            result = engine.run(state, lambda _task: True)
            self.assertEqual(result.status, "failed")
            self.assertTrue(any(event["event"] == "safe_stop" for event in result.audit))
            self.assertFalse((Path(directory).parent / "escape.txt").exists())

    def test_replan_invalidates_only_descendants(self):
        tasks = [
            TaskSpec("source", (), handler="make"),
            TaskSpec("child", ("source",), handler="make"),
            TaskSpec("other", (), handler="make"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            engine = WorkflowEngine(tasks, {"make": lambda _context: {"value": 1}}, Path(directory))
            state = engine.run(WorkflowState.create("replan", {}, tasks), lambda _task: True)
            invalidated = engine.replan(state, "source", {"value": 2})
            self.assertEqual(invalidated, ["child"])
            self.assertEqual(state.task_status["child"], "pending")
            self.assertEqual(state.task_status["other"], "succeeded")
            self.assertEqual(state.task_outputs["source"], {"value": 2})

    def test_parallel_failure_discards_sibling_and_cancels_workflow(self):
        tasks = [
            TaskSpec("start", (), handler="start"),
            TaskSpec("bad", ("start",), handler="bad"),
            TaskSpec("good", ("start",), handler="good"),
        ]
        handlers = {
            "start": lambda _context: {},
            "bad": lambda _context: (_ for _ in ()).throw(RuntimeError("failure")),
            "good": lambda _context: {"artifacts": {"proposal.md": "created in sibling"}},
        }
        with tempfile.TemporaryDirectory() as directory:
            engine = WorkflowEngine(tasks, handlers, Path(directory))
            result = engine.run(WorkflowState.create("rollback", {}, tasks), lambda _task: True)
            self.assertEqual(result.status, "failed")
            self.assertFalse((Path(directory) / "artifacts" / "good" / "proposal.md").exists())
            self.assertEqual(result.task_status["good"], "cancelled")

    def test_ambiguous_scenario_withholds_implementation(self):
        with tempfile.TemporaryDirectory() as directory:
            tasks, handlers, context = build_scenario("ambiguous", Path(directory))
            engine = WorkflowEngine(tasks, handlers, Path(directory) / "run")
            result = engine.run(WorkflowState.create("ambiguous", context, tasks), lambda _task: True)
            artifacts = Path(directory) / "run" / "artifacts"
            self.assertEqual(result.status, "completed")
            self.assertTrue((artifacts / "implement" / "requirements-clarification.md").is_file())
            self.assertFalse(any(artifacts.rglob("*.py")))
            report = json.loads((artifacts / "release_readiness" / "engineering-summary.json").read_text())
            self.assertEqual(report["release_status"], "blocked_pending_clarification")


if __name__ == "__main__":
    unittest.main()