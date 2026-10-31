import json
from pathlib import Path
import time
import threading
import pytest
from liquidity.book import OrderBook
from liquidity.models import Instrument


@pytest.fixture
def instrument():
    return Instrument("binance", "BTCUSDT", tick="0.1", qty_step="0.001")


@pytest.fixture
def book(instrument):
    b = OrderBook(instrument)
    b.binance_snapshot(
        {
            "lastUpdateId": 100,
            "bids": [["99", "10"], ["98", "10"]],
            "asks": [["101", "20"], ["102", "10"]],
        },
        0,
    )
    b.binance_delta({"U": 99, "u": 100, "pu": 98, "b": [], "a": []}, 1)
    return b


@pytest.fixture(autouse=True)
def no_unrequested_live_collector(monkeypatch):
    monkeypatch.setenv("WHALE_ORDERS_ENABLED", "false")
    monkeypatch.setenv("WHALE_LIQUIDITY_ENABLED", "false")
    monkeypatch.setenv("WHALE_LIQUIDITY_MODE", "live")
    monkeypatch.setenv("WHALE_LIQUIDITY_ALERTS", "false")
    monkeypatch.setenv("WHALE_LIQUIDITY_JOURNAL", "")


@pytest.fixture(autouse=True)
def test_admin_token(monkeypatch):
    monkeypatch.setenv("WHALE_ADMIN_TOKEN", "unit-test-admin-password-32-characters")


@pytest.fixture(autouse=True)
def block_external_network(monkeypatch):
    import ipaddress
    import socket

    connect, resolve = socket.socket.connect, socket.getaddrinfo
    attempts = []

    def allowed(host):
        if host is None or host == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def guarded_connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not allowed(address[0]):
            attempts.append(str(address[0]))
            raise RuntimeError("External network is forbidden in unit tests")
        return connect(sock, address)

    def guarded_resolve(host, *args, **kwargs):
        if not allowed(host):
            attempts.append(str(host))
            raise RuntimeError("External DNS is forbidden in unit tests")
        return resolve(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_resolve)
    yield
    assert not attempts, "Unexpected external network attempt(s): " + ", ".join(attempts)


# Record collection, test phases and session/CPU clocks separately. JUnit testcase
# totals alone cannot diagnose a gap outside the tests (e.g. collection or suspension).

_timing = {}


def pytest_addoption(parser):
    parser.addoption("--timing-report", default=None, help="Write lifecycle timing JSON")


def pytest_sessionstart(session):
    _timing.update(
        start=time.perf_counter(), cpu_start=time.process_time(), phases={}, tests=[], collection=0
    )

    _timing.update(
        clock_gaps=[], active_test=None, active_phase="collection", heartbeat_stop=threading.Event()
    )
    _sample_clock_gap()
    worker = threading.Thread(target=_heartbeat, daemon=True)
    _timing["heartbeat_thread"] = worker
    worker.start()


def _sample_clock_gap():
    wall, cpu, monotonic = time.perf_counter(), time.process_time(), time.monotonic()
    previous = _timing.get("heartbeat_last")
    if previous and wall - previous[0] > 5:
        _timing["clock_gaps"].append(
            {
                "wall_gap_seconds": wall - previous[0],
                "cpu_seconds_in_gap": cpu - previous[1],
                "monotonic_gap_seconds": monotonic - previous[2],
                "active_test": _timing.get("active_test"),
                "active_phase": _timing.get("active_phase"),
            }
        )
    _timing["heartbeat_last"] = (wall, cpu, monotonic)


def _heartbeat():
    while not _timing["heartbeat_stop"].wait(0.5):
        _sample_clock_gap()


def pytest_runtest_logstart(nodeid, location):
    _timing.update(active_test=nodeid, active_phase="setup")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _timing["active_phase"] = "call"
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    _timing["active_phase"] = "teardown"
    yield


def enforce_wall_limit(report, limit):
    if report.outcome == "passed" and limit > 0 and report.duration > limit:
        report.outcome = "failed"
        report.longrepr = (
            f"{report.when} exceeded the {limit}s wall-time limit ({report.duration:.3f}s). "
            "Check test-clock heartbeat data for an execution/host pause."
        )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    marker = item.get_closest_marker("timeout")
    limit = (
        float(marker.args[0]) if marker and marker.args else float(item.config.getini("timeout"))
    )
    enforce_wall_limit(report, limit)


def pytest_collection(session):
    _timing["collection_start"] = time.perf_counter()


def pytest_collection_finish(session):
    _timing["collection"] = time.perf_counter() - _timing["collection_start"]


def pytest_runtest_logreport(report):
    _timing["phases"][report.when] = _timing["phases"].get(report.when, 0) + report.duration
    _timing["tests"].append(
        {
            "test": report.nodeid,
            "phase": report.when,
            "seconds": report.duration,
            "outcome": report.outcome,
        }
    )


def pytest_sessionfinish(session, exitstatus):
    _timing["heartbeat_stop"].set()
    _timing["heartbeat_thread"].join(timeout=1)
    _sample_clock_gap()
    total = time.perf_counter() - _timing["start"]
    test_time = sum(_timing["phases"].values())
    report = {
        "session_seconds": total,
        "process_cpu_seconds": time.process_time() - _timing["cpu_start"],
        "collection_seconds": _timing["collection"],
        "test_phase_seconds": _timing["phases"],
        "unattributed_session_seconds": max(0, total - test_time - _timing["collection"]),
        "exit_status": int(exitstatus),
        "clock_gaps": _timing["clock_gaps"],
        "clock_implementations": {
            name: time.get_clock_info(name).implementation for name in ("perf_counter", "monotonic")
        },
        "external_network": "blocked; loopback test servers only",
        "slowest_phases": sorted(_timing["tests"], key=lambda r: r["seconds"], reverse=True)[:20],
    }
    path = session.config.getoption("--timing-report")
    if path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2), encoding="utf-8")


def pytest_report_header(config):
    return "External network blocked; per-test timeout and explicit lifecycle timing enabled"
