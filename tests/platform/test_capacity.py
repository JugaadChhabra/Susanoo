"""Measured control-plane capacity; protocol clients are not business-runtime qualification."""

import json
import os
import platform
import socket
import statistics
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import uvicorn
import psutil
from conftest import PASSWORD, approve, launch, result, version

from susanoo_api.platform.database import transaction


@pytest.mark.parametrize("concurrency", [1, 3])
def test_twenty_case_protocol_concurrency(suite, concurrency):
    cases = [version(suite, {**suite["case"]["data"]}) for _ in range(20)]
    collection = version(
        suite, {"kind": "suite", "case_version_ids": [row["id"] for row in cases], "limits": {"max_concurrency": 3}}
    )
    approve(suite, collection)
    connection = version(
        suite,
        {
            **suite["connection"]["data"],
            "capabilities": {
                **suite["connection"]["data"]["capabilities"],
                "max_concurrency": 3,
                "isolation_tested": True,
            },
        },
    )
    suite["owner"].ok(
        "POST",
        f"/projects/{suite['project']['id']}/jobs",
        {
            "kind": "preflight",
            "version_ids": [connection["id"]],
        },
        headers={"Idempotency-Key": "parallel-preflight"},
    )
    runner = suite["runner"]
    check = runner.claim("preflight")
    runner.event(
        check,
        1,
        "job_result",
        {
            "invocation_ok": True,
            "observer_ok": True,
            "cleanup_ok": True,
            "isolation_verified": True,
            "max_concurrency": concurrency,
        },
    )
    runner.finalize(check)
    suite["body"].update(
        suite_version_id=collection["id"],
        connection_version_id=connection["id"],
        limits={"max_concurrency": concurrency},
    )
    run = launch(suite)
    completed = 0
    namespaces = set()
    while completed < 20:
        claims = [runner.claim("outcome_suite") for _ in range(min(concurrency, 20 - completed))]
        assert runner.send("/claims", {"supported_job_kinds": ["outcome_suite"]}).status_code == 204
        for claim in claims:
            namespaces.add(claim["namespace"])
            assert result(suite, claim)[0].is_success
            runner.event(claim, 1, "progress", {"code": "protocol_test_only"})
            runner.finalize(claim)
            completed += 1
    assert len(namespaces) == 20
    assert suite["owner"].ok("GET", f"/runs/{run['id']}")["counts"]["completed"] == 20


def test_capacity_ten_thousand_attempts_real_http(suite):
    process = psutil.Process()
    cpu_before = process.cpu_times()
    max_rss = process.memory_info().rss
    run = launch(suite)
    with transaction() as db:
        db.execute(
            """insert into platform.attempts(workspace_id,project_id,run_id,namespace)
            select %s,%s,%s,'capacity-'||gen_random_uuid()::text from generate_series(1,9999)""",
            (suite["workspace"]["id"], suite["project"]["id"], run["id"]),
        )
        postgres = db.execute("select version() version").fetchone()["version"]
        database_bytes = db.execute("select pg_database_size(current_database()) bytes").fetchone()["bytes"]
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(suite["app"], log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        with httpx.Client(base_url=f"http://127.0.0.1:{port}/api/v1", timeout=10) as client:
            csrf = client.get("/auth/csrf").json()["csrf_token"]
            response = client.post(
                "/auth/login",
                json={"email": "owner@test.local", "password": PASSWORD},
                headers={"Origin": "http://localhost:3000", "X-CSRF-Token": csrf},
            )
            assert response.is_success
            timings = {}
            for name, path in {
                "run_status": f"/runs/{run['id']}",
                "case_list": f"/projects/{suite['project']['id']}/versions?kind=case&limit=50",
            }.items():
                values = []
                for _ in range(30):
                    started = time.perf_counter()
                    response = client.get(path)
                    values.append((time.perf_counter() - started) * 1000)
                    max_rss = max(max_rss, process.memory_info().rss)
                    assert response.is_success, response.text
                timings[name] = {
                    "samples": len(values),
                    "p50_ms": round(statistics.median(values), 2),
                    "p95_ms": round(sorted(values)[28], 2),
                    "max_ms": round(max(values), 2),
                }
                assert timings[name]["p95_ms"] < 1000
            view = client.get(f"/runs/{run['id']}").json()
            assert view["counts"]["scheduled"] == 10000 and len(view["attempts"]) == 200
            assert view["attempts_next_cursor"]
            assert (
                len(
                    client.get(f"/runs/{run['id']}/attempts?after={view['attempts_next_cursor']}&limit=200").json()[
                        "items"
                    ]
                )
                == 200
            )
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()
        assert not thread.is_alive()
    output = Path(__file__).resolve().parents[2] / ".platform/capacity-report.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "measured_at": datetime.now(UTC).isoformat(),
                "attempt_records": 10000,
                "transport": "real loopback HTTP / uvicorn / PostgreSQL; synthetic records; no customer runtime",
                "host": platform.platform(),
                "python": platform.python_version(),
                "postgres": postgres,
                "resources": {
                    "api_test_process_peak_rss_bytes": max_rss,
                    "database_size_bytes": database_bytes,
                    "api_test_process_cpu_seconds": round(
                        process.cpu_times().user - cpu_before.user + process.cpu_times().system - cpu_before.system, 3
                    ),
                    "logical_cpus": os.cpu_count(),
                    "postgres_process_rss_measured": False,
                },
                "metrics": timings,
                "sla_claimed": False,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
