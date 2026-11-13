"""Provision and exercise a synthetic Sim project on the LOCAL Susanoo platform only."""

import asyncio
import json
import sys
import uuid

import httpx
from calibrate_platform import Calibration
from susanoo_contracts import content_hash
from susanoo_runner.local import SimulatorServer
from susanoo_runner.platform import PlatformClient, PlatformProfile, execute_claim
from susanoo_sdk import JsonlAgentProcess
from susanoo_simulator.adapter import SimulatorFixtureAdapter

from paths import ROOT, REPO, STATE, PRIVATE
from paths import credentials_file, ensure_private_state

ensure_private_state()
STATE_FILE = PRIVATE / "platform.json"
state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
binding = json.loads((STATE / ".private/binding.json").read_text())
sample = json.loads((REPO / "examples/collections/suite.json").read_text(encoding="utf-8"))
accounts = json.loads(credentials_file().read_text())["synthetic_local_accounts"]


def save():
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


class Session:
    def __init__(self, role):
        self.http = httpx.Client(
            base_url="http://127.0.0.1:8000/api/v1/", timeout=30, headers={"Origin": "http://localhost:3000"}
        )
        self.http.headers["X-CSRF-Token"] = self.http.get("auth/csrf").json()["csrf_token"]
        account = next(a for a in accounts if a["email"] == f"{role}@susanoo.local")
        self.identity = self.call("POST", "auth/login", account)

    def call(self, method, path, body=None, key=None):
        response = self.http.request(method, path, json=body, headers={"Idempotency-Key": key} if key else {})
        if response.is_error:
            raise RuntimeError(f"{method} {path}: HTTP {response.status_code}: {response.text[:600]}")
        return response.json()


owner, reviewer = Session("owner"), Session("reviewer")
if "project" not in state:
    projects = owner.call("GET", "projects")["items"]
    project = next((p for p in projects if p["name"] == "Sim Local Integration - Synthetic"), None)
    if project is None:
        project = owner.call(
            "POST",
            "projects",
            {
                "workspace_id": projects[0]["workspace_id"],
                "name": "Sim Local Integration - Synthetic",
                "workflow": "Synthetic Sim promise-to-pay",
                "policy": {"mode": "synthetic", "upload_fields": ["count", "amount_minor", "currency"]},
            },
        )
    state["project"] = project
    state["versions"] = {}
    save()
project = state["project"]
for session, role in [(owner, "owner"), (reviewer, "reviewer")]:
    owner.call(
        "POST",
        f"workspaces/{project['workspace_id']}/members",
        {
            "user_id": session.identity["user_id"],
            "roles": ["owner", "engineer"] if role == "owner" else [role],
            "hidden_access": True,
            "project_id": project["id"],
        },
    )


def version(logical, data, hidden=False, approve=False):
    key = logical + ":" + content_hash(data)
    row = state["versions"].get(key)
    if row is None:
        row = owner.call(
            "POST",
            f"projects/{project['id']}/versions",
            {
                "logical_id": logical,
                "title": "Synthetic Sim " + logical,
                "data": data,
                "hidden": hidden,
                "provenance": {"source": "local synthetic integration harness", "not_customer_approval": "true"},
            },
        )
        state["versions"][key] = row
        save()
    if approve and not row.get("approved"):
        reviewer.call(
            "POST",
            f"versions/{row['id']}/approve",
            {
                "content_hash": row["content_hash"],
                "rationale": (
                    "Automated synthetic fixture approval by the local reviewer account; not external human acceptance."
                ),
            },
        )
        row["approved"] = True
        save()
    return row


requirements = {}
for requirement in sample["requirements"]:
    requirements[requirement["id"]] = version(
        requirement["id"],
        {
            "kind": "requirement",
            "source_clause": requirement["id"],
            "expected": requirement["text"],
            "severity": requirement["severity"],
            "owner": "Local synthetic reviewer",
        },
        approve=True,
    )
calibration = Calibration(ROOT, owner, state, save, version)
cases = []
for case in sample["cases"]:
    hidden = case["visibility"] == "hidden"
    fixture = version(case["id"] + "-fixture", {"kind": "fixture", **case["fixture"]}, hidden=hidden)
    checks = []
    for check in case["checks"]:
        data = {k: v for k, v in check.items() if k not in {"id", "requirement_id"}}
        data.update(kind="check", requirement_version_id=requirements[check["requirement_id"]]["id"])
        data["entity"] = next(o["kind"] for o in case["observations"] if o["name"] == check["observation"])
        raw_check = version(case["id"] + "-" + check["id"], data, hidden=hidden)
        if check.get("severity") == "critical":
            grader_id = calibration.check(case, raw_check, fixture["id"])
            data["grader_version_id"] = grader_id
            raw_check = version(case["id"] + "-" + check["id"], data, hidden=hidden)
        checks.append(raw_check["id"])
    cases.append(
        version(
            case["id"],
            {
                "kind": "case",
                "fixture_version_id": fixture["id"],
                "check_version_ids": checks,
                "script": case["script"],
                "required": case["required"],
                "visibility": case["visibility"],
            },
            hidden=hidden,
        )["id"]
    )
calibration.close()
suite = version("full-twenty-case-suite", {"kind": "suite", "case_version_ids": cases}, approve=True)
control_suite = version(
    "single-case-negative-control-suite", {"kind": "suite", "case_version_ids": cases[:1]}, approve=True
)
operations = sample["cases"][0]["fixture"]["recipe"]["seed"]["actors"]["agent"]["operations"]
environment = version(
    "local-simulator",
    {
        "kind": "environment",
        "adapter": "susanoo_simulator",
        "version": "1.0",
        "allowed_operations": operations,
        "identity_profile": "synthetic-agent",
        "fixture_version_id": next(v["id"] for v in state["versions"].values() if v["logical_id"] == "C01-fixture"),
    },
    approve=True,
)
connections = {}
for mode in ["correct", "false_completion", "wrong_amount"]:
    connections[mode] = version(
        "sim-" + mode,
        {
            "kind": "connection",
            "family": "I06",
            "adapter": "sim-local-jsonl",
            "capabilities": {
                "invoke": True,
                "sessions": True,
                "multi_turn": True,
                "observe": True,
                "tool_events": True,
            },
            "mapping": {
                "workflow_id": binding["workflow_id"],
                "deployment_version": str(binding["deployment_version"]),
                "graph_hash": binding["graph_hash"],
                "mode": mode,
            },
        },
        approve=True,
    )
if "runner_token" not in state:
    registration = owner.call("POST", f"projects/{project['id']}/runner-registration")["token"]
    registered = owner.call(
        "POST",
        "runner/register",
        {
            "registration_token": registration,
            "name": "Local Sim JSONL runner - synthetic only",
            "version": "2.0-local",
            "capabilities": ["outcome_suite", "preflight"],
        },
    )
    state["runner_token"] = registered["token"]
    save()
profile = PlatformProfile(
    allowed_connection_adapters=["sim-local-jsonl"],
    allowed_connection_hashes=[content_hash(c["data"]) for c in connections.values()],
    allowed_operations=operations,
)
(STATE / ".private/approved-profile.json").write_text(profile.model_dump_json(indent=2), encoding="utf-8")
client = PlatformClient("http://127.0.0.1:8000/api/v1", state["runner_token"])


async def execute(claim, simulator, outage=False):
    connection = claim.agent_context.connection
    if connection is None:
        connection = next(v.data for v in claim.execution_manifest.versions if v.kind == "connection")
    mapping = connection.mapping
    if (
        mapping["workflow_id"] != binding["workflow_id"]
        or mapping["graph_hash"] != binding["graph_hash"]
        or mapping["deployment_version"] != str(binding["deployment_version"])
    ):
        raise PermissionError("Platform connection does not match the hash-pinned local Sim binding")
    fixtures = SimulatorFixtureAdapter(simulator.base_url, simulator.admin_key, observer_outage=outage)
    try:
        return await execute_claim(
            client,
            claim,
            profile=profile,
            journal_path=PRIVATE / f"platform-{claim.attempt_id}.sqlite3",
            agent_factory=lambda: JsonlAgentProcess(
                [
                    sys.executable,
                    str(ROOT / "serve.py"),
                    "--mode",
                    mapping["mode"],
                    "--binding-pin",
                    content_hash({k: binding[k] for k in ["workflow_id", "deployment_version", "graph_hash"]}),
                ],
                cwd=str(ROOT),
                env={"SUSANOO_SIM_STATE_DIR": str(STATE)},
            ),
            fixtures=fixtures,
        )
    finally:
        await fixtures.aclose()


async def main():
    report = {
        "scope": "Local platform/API/PostgreSQL/runner/JSONL/Sim/synthetic simulator; no external qualification",
        "project_id": project["id"],
        "scenarios": [],
    }
    with SimulatorServer() as simulator:
        for mode, outage, selected, expected_gate, expected_count in [
            ("correct", False, suite, "READY_FOR_ACCEPTANCE", 20),
            ("false_completion", False, control_suite, "BLOCKED", 1),
            ("wrong_amount", False, control_suite, "BLOCKED", 1),
            ("correct", True, control_suite, "NEEDS_REVIEW", 1),
        ]:
            connection = connections[mode]
            owner.call(
                "POST",
                f"projects/{project['id']}/jobs",
                {"kind": "preflight", "version_ids": [connection["id"], environment["id"], selected["id"]]},
                key=str(uuid.uuid4()),
            )
            claim = client.claim(["preflight"])
            if claim is None or claim.kind != "preflight":
                raise RuntimeError("Expected a new preflight claim")
            await execute(claim, simulator)
            run = owner.call(
                "POST",
                f"projects/{project['id']}/runs",
                {
                    "suite_version_id": selected["id"],
                    "connection_version_id": connection["id"],
                    "environment_version_id": environment["id"],
                },
                key=str(uuid.uuid4()),
            )
            for _ in range(expected_count):
                claim = client.claim(["outcome_suite"])
                if claim is None or str(claim.run_id) != run["id"]:
                    raise RuntimeError("Expected a fresh claim for the exact launched run; refusing unrelated work")
                result = await execute(claim, simulator, outage)
                print(
                    json.dumps(
                        {
                            "case": str(claim.agent_context.case_id),
                            "verdict": result.business_verdict,
                            "execution": result.execution_state,
                        }
                    ),
                    flush=True,
                )
            persisted = reviewer.call("GET", f"runs/{run['id']}")
            item = {
                "mode": mode,
                "observer_outage": outage,
                "run_id": run["id"],
                "gate": persisted["gate"],
                "counts": persisted["counts"],
                "expected_gate": expected_gate,
            }
            report["scenarios"].append(item)
            (STATE / "platform-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps(item), flush=True)
            if persisted["gate"] != expected_gate:
                raise AssertionError("Stored platform gate does not match the actual expected scenario")
    print("PASS: real local platform claim, evidence upload, finalization and stored gates", flush=True)


try:
    asyncio.run(main())
finally:
    client.close()
    owner.http.close()
    reviewer.http.close()
