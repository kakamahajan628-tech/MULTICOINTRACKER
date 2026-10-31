"""Run offline tests with phase timing, durations, JUnit and a whole-process deadline."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def wait_with_wall_deadline(process, deadline, *, started=None, clock=time.perf_counter):
    started = clock() if started is None else started
    while process.poll() is None:
        remaining = deadline - (clock() - started)
        if remaining <= 0:
            process.kill()
            process.wait()
            return 124, "whole_process_timeout"
        try:
            process.wait(timeout=min(0.5, remaining))
        except subprocess.TimeoutExpired:
            continue
    # Windows kernel waits can exclude low-power time; do not accept a late success.
    if clock() - started > deadline:
        return 124, "wall_deadline_exceeded_on_resume"
    return process.returncode, "passed" if process.returncode == 0 else "failed"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deadline", type=int, default=300)
    args, extra = parser.parse_known_args()
    if extra[:1] == ["--"]:
        extra.pop(0)
    root = Path(__file__).resolve().parents[1]
    reports = root / "reports"
    reports.mkdir(exist_ok=True)
    started = time.perf_counter()
    result = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "deadline_seconds": args.deadline,
    }
    command = [
        sys.executable,
        "-m",
        "pytest",
        "--durations=0",
        "--timing-report=reports/test-timing.json",
        "--junitxml=reports/pytest-hardening.xml",
        *extra,
    ]
    print("Running offline tests; full output: reports/pytest-console.log", flush=True)
    with (reports / "pytest-console.log").open("w", encoding="utf-8") as log:
        with subprocess.Popen(
            command, cwd=root, stdout=log, stderr=subprocess.STDOUT, env=os.environ.copy()
        ) as process:
            code, status = wait_with_wall_deadline(process, args.deadline, started=started)
            result.update(status=status, child_returncode=process.returncode)
    result.update(exit_code=code, wall_seconds=time.perf_counter() - started)
    (reports / "test-run.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    lines = (
        (reports / "pytest-console.log").read_text(encoding="utf-8", errors="replace").splitlines()
    )
    print("\n".join(lines[-60:] if code else lines[-3:]))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
