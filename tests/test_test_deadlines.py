import importlib.util
from pathlib import Path
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location(
    "test_runner", Path(__file__).parents[1] / "scripts/run_tests.py"
)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_success_after_resume_cannot_bypass_wall_deadline():
    class Process:
        returncode = 0

        def poll(self):
            return 0

    values = iter((0, 400))
    assert runner.wait_with_wall_deadline(Process(), 300, clock=lambda: next(values)) == (
        124,
        "wall_deadline_exceeded_on_resume",
    )


def test_running_child_is_killed_when_wall_deadline_expired():
    class Process:
        returncode = None
        killed = False

        def poll(self):
            return None

        def kill(self):
            self.killed = True

        def wait(self):
            self.returncode = -1

    values = iter((0, 400))
    child = Process()
    assert runner.wait_with_wall_deadline(child, 300, clock=lambda: next(values)) == (
        124,
        "whole_process_timeout",
    )
    assert child.killed


def test_completed_test_phase_cannot_hide_wall_timeout_after_resume():
    import conftest

    report = SimpleNamespace(outcome="passed", when="call", duration=900)
    conftest.enforce_wall_limit(report, 120)
    assert report.outcome == "failed" and "execution/host pause" in report.longrepr
