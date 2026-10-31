"""Fail-closed OSV checks bound to exact input lock hashes and every queried pin."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import httpx
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

SERVICE = "https://api.osv.dev/v1/querybatch"


def read_locks(paths):
    locks, pins = [], []
    for path in paths:
        path = Path(path)
        raw = path.read_bytes()
        locks.append({"file": path.name, "sha256": hashlib.sha256(raw).hexdigest()})
        for line_number, line in enumerate(raw.decode("utf-8-sig").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            requirement = Requirement(line)
            specs = list(requirement.specifier)
            if (
                requirement.url
                or len(specs) != 1
                or specs[0].operator != "=="
                or "*" in specs[0].version
            ):
                raise ValueError(f"{path.name}:{line_number}: audit requires an exact == pin")
            Version(specs[0].version)
            pins.append(
                {
                    "name": canonicalize_name(requirement.name),
                    "version": specs[0].version,
                    "marker": str(requirement.marker) if requirement.marker else None,
                    "lock": path.name,
                    "line": line_number,
                }
            )
    if not pins:
        raise ValueError("No locked packages to audit")
    return locks, pins


def audit(paths, client):
    locks, pins = read_locks(paths)
    queries = [
        {"package": {"name": pin["name"], "ecosystem": "PyPI"}, "version": pin["version"]}
        for pin in pins
    ]
    report = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "service": SERVICE,
        "locks": locks,
        "packages": len(queries),
        "pins": pins,
        "coverage": "Every lock entry, including markers inactive on the audit host",
        "findings": [],
    }
    pending = list(enumerate(queries))
    results = {i: {} for i in range(len(queries))}
    seen = set()
    pages = 0
    while pending:
        pages += 1
        if pages > 20:
            raise ValueError("OSV pagination exceeded the bounded audit budget")
        response = client.post(
            SERVICE, json={"queries": [query for _, query in pending]}, timeout=30
        )
        response.raise_for_status()
        batch = response.json()["results"]
        if not isinstance(batch, list) or len(batch) != len(pending):
            raise ValueError("Incomplete OSV response")
        next_pending = []
        for (index, query), result in zip(pending, batch, strict=True):
            for vulnerability in result.get("vulns", []):
                results[index][vulnerability["id"]] = vulnerability
            token = result.get("next_page_token")
            if token:
                if not isinstance(token, str) or (index, token) in seen:
                    raise ValueError("Invalid or repeated OSV pagination token")
                seen.add((index, token))
                next_pending.append((index, {**query, "page_token": token}))
        pending = next_pending
    report["pages"] = pages
    report["findings"] = [
        {**pin, "vulnerabilities": list(results[i].values())}
        for i, pin in enumerate(pins)
        if results[i]
    ]
    report["status"] = "vulnerable" if report["findings"] else "checked"
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", action="append", type=Path)
    parser.add_argument("--output", type=Path, default=Path("reports/dependency-audit.json"))
    args = parser.parse_args(argv)
    paths = args.lock or [Path("requirements.lock")]
    targets = [
        {"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for path in paths
        if path.is_file()
    ]
    try:
        with httpx.Client() as client:
            report = audit(paths, client)
        code = 1 if report["findings"] else 0
    except Exception as exc:
        # The audit must fail rather than certify a stale or incomplete response.
        report = {
            "status": "unverified",
            "error_type": type(exc).__name__,
            "locks": targets,
            "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        code = 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "pins"}, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
