"""Reproduce source provenance from separately cloned repositories; never copies code."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys

SOURCES = {
    "order-book-stream": "aryan1803/Order-Book-Stream-Visualization",
    "flowmap": "nazmiefearmutcu/flowmap",
    "orderbook-heatmap": "mamonet/orderbook-heatmap",
    "liqmap": "bendizziesmith/liqmap",
    "order-flow-screener": "zadescoxp/order-flow-screener",
    "hft": "arvin-shafiei/hft",
    "crypto-orderbook": "jose-donato/crypto-orderbook",
    "hftbacktest": "nkaz001/hftbacktest",
}


def audit(root):
    rows = []
    for folder, repository in SOURCES.items():
        path = Path(root) / folder
        commit = subprocess.check_output(
            ["git", "-C", str(path), "rev-parse", "HEAD"], text=True
        ).strip()
        tracked = subprocess.check_output(
            ["git", "-C", str(path), "ls-files"], text=True
        ).splitlines()
        files = [
            p
            for p in tracked
            if any(x in Path(p).name.lower() for x in ("license", "copying", "notice"))
            or Path(p).name
            in ("pyproject.toml", "requirements.txt", "package.json", "go.mod", "Cargo.toml")
        ]
        rows.append(
            {
                "repository": repository,
                "url": f"https://github.com/{repository}",
                "commit": commit,
                "files": [
                    {"path": p, "sha256": hashlib.sha256((path / p).read_bytes()).hexdigest()}
                    for p in files
                ],
            }
        )
    return rows


if __name__ == "__main__":
    result = audit(sys.argv[1])
    Path("reports/source-provenance.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
