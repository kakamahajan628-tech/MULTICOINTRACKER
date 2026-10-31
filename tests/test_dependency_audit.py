import hashlib
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

spec = importlib.util.spec_from_file_location(
    "dependency_audit", Path(__file__).parents[1] / "scripts/audit_dependencies.py"
)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def test_exact_pins_hashes_inactive_markers_and_pagination_are_recorded(tmp_path):
    path = tmp_path / "requirements.lock"
    raw = b'example==1.2.3; platform_system == "Linux"\n'
    path.write_bytes(raw)
    requests = []

    def response(request):
        query = json.loads(request.content)["queries"][0]
        requests.append(query)
        return httpx.Response(
            200,
            json={
                "results": [{"vulns": [{"id": "V2"}]}]
                if query.get("page_token")
                else [{"vulns": [{"id": "V1"}], "next_page_token": "next"}]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        report = audit.audit([path], client)
    assert report["locks"][0]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert report["pins"][0]["version"] == "1.2.3" and report["pins"][0]["marker"]
    assert report["status"] == "vulnerable" and report["pages"] == 2
    assert {v["id"] for v in report["findings"][0]["vulnerabilities"]} == {"V1", "V2"}
    assert requests[1]["page_token"] == "next"


@pytest.mark.parametrize(
    "requirement",
    [
        "example>=1",
        "example==1.*",
        "example~=1.2",
        "example==1,!=1.5",
        "example @ https://example.test/file.whl",
    ],
)
def test_nonexact_requirements_are_never_certified(tmp_path, requirement):
    path = tmp_path / "bad.lock"
    path.write_text(requirement)
    with pytest.raises(ValueError):
        audit.read_locks([path])


def test_incomplete_response_and_network_failure_fail_audit(tmp_path, monkeypatch):
    path = tmp_path / "requirements.lock"
    path.write_text("example==1.0\n")
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"results": []}))
    ) as client:
        with pytest.raises(ValueError, match="Incomplete"):
            audit.audit([path], client)

    def fail(*args):
        raise httpx.ConnectError("unavailable")

    monkeypatch.setattr(audit, "audit", fail)
    output = tmp_path / "audit.json"
    assert audit.main(["--lock", str(path), "--output", str(output)]) == 2
    assert json.loads(output.read_text())["status"] == "unverified"


def test_programming_errors_are_not_hidden_as_retryable_source_failures(tmp_path):
    import asyncio
    from whale_watcher.core import Store
    from whale_watcher.recovery import Recovery

    async def exercise():
        store = Store("sqlite:///" + (tmp_path / "internal.sqlite").as_posix())
        recovery = Recovery(store)

        async def broken():
            raise TypeError("SECRET must not be serialized")

        with pytest.raises(TypeError):
            await recovery.run("adapter", broken)
        assert recovery.health["adapter"]["state"] == "internal_error"
        assert "SECRET" not in json.dumps(recovery.health)
        store.engine.dispose()

    asyncio.run(exercise())
