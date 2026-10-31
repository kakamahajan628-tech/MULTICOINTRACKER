"""Freeze the integrated runtime closure and license metadata; retain platform markers."""

from importlib.metadata import distribution
import json
from pathlib import Path
import tomllib
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

roots = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8-sig"))["project"][
    "dependencies"
]
pending = [Requirement(x) for x in roots]
rows, visited = {}, set()
while pending:
    requirement = pending.pop()
    name = canonicalize_name(requirement.name)
    extras = tuple(sorted(requirement.extras))
    if (name, extras) in visited:
        continue
    visited.add((name, extras))
    d = distribution(name)
    rows[name] = {
        "name": d.metadata["Name"],
        "version": d.version,
        "license": d.metadata.get("License-Expression") or d.metadata.get("License"),
        "license_classifiers": [
            c for c in d.metadata.get_all("Classifier", []) if c.startswith("License ::")
        ],
    }
    for raw in d.requires or []:
        child = Requirement(raw)
        if child.marker is None or any(child.marker.evaluate({"extra": e}) for e in ("", *extras)):
            pending.append(child)

# ccxt's Linux event-loop dependency is not installed on the Windows test host.
# Version and marker are copied from its actual installed Requires-Dist metadata.
rows["uvloop"] = {
    "name": "uvloop",
    "version": "0.22.1",
    "license": "MIT (PyPI metadata)",
    "license_source": "PyPI distribution metadata; Linux wheel not executed here",
}
markers = {
    "winloop": 'platform_system == "Windows" and implementation_name == "cpython" and python_version < "3.15" and platform_machine in "AMD64 ARM64"',
    "uvloop": 'platform_system != "Windows" and implementation_name == "cpython"',
    "orjson": 'implementation_name == "cpython" and python_version < "3.15"',
    "coincurve": 'implementation_name == "cpython" and python_version < "3.14"',
}
lines = [
    "# Integrated exact runtime closure; Windows 3.12 tested. Linux uvloop is marker-specific."
]
for name, d in sorted(rows.items()):
    line = f"{name}=={d['version']}"
    if name in markers:
        line += "; " + markers[name]
    lines.append(line)
Path("requirements.lock").write_text("\n".join(lines) + "\n", encoding="utf-8")
Path("reports/dependencies.json").write_text(
    json.dumps(list(rows.values()), indent=2), encoding="utf-8"
)
print(f"Locked {len(rows)} runtime distributions with platform markers.")
