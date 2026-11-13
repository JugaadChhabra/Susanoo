"""Persistent, local-only worker for explicitly pinned Sim connections."""

import asyncio
import json
import sys

from susanoo_contracts import content_hash
from susanoo_runner.local import SimulatorServer
from susanoo_runner.platform import PlatformClient, PlatformProfile, execute_claim
from susanoo_sdk import JsonlAgentProcess
from susanoo_simulator.adapter import SimulatorFixtureAdapter

from paths import ROOT, STATE, PRIVATE


async def execute(client, claim, simulator, profile, binding, *, outage=False):
    connection = claim.agent_context.connection
    if connection is None:
        connection = next(v.data for v in claim.execution_manifest.versions if v.kind == "connection")
    mapping = connection.mapping
    if connection.adapter != "sim-local-jsonl" or content_hash(connection) not in profile.allowed_connection_hashes:
        raise PermissionError("Connection is not locally approved")
    if any(str(mapping.get(k)) != str(binding[k]) for k in ["workflow_id", "deployment_version", "graph_hash"]):
        raise PermissionError("Connection does not match the pinned Sim deployment")
    fixtures = SimulatorFixtureAdapter(simulator.base_url, simulator.admin_key, observer_outage=outage)
    try:
        pin = content_hash({k: binding[k] for k in ["workflow_id", "deployment_version", "graph_hash"]})
        return await execute_claim(
            client,
            claim,
            profile=profile,
            journal_path=PRIVATE / f"platform-{claim.attempt_id}.sqlite3",
            agent_factory=lambda: JsonlAgentProcess(
                [sys.executable, str(ROOT / "serve.py"), "--mode", mapping["mode"], "--binding-pin", pin],
                cwd=str(ROOT),
                env={"SUSANOO_SIM_STATE_DIR": str(STATE)},
            ),
            fixtures=fixtures,
        )
    finally:
        await fixtures.aclose()


async def main():
    state = json.loads((STATE / ".private/platform.json").read_text())
    binding = json.loads((STATE / ".private/binding.json").read_text())
    profile = PlatformProfile.model_validate_json((STATE / ".private/approved-profile.json").read_text())
    client = PlatformClient("http://127.0.0.1:8000/api/v1", state["runner_token"])
    try:
        with SimulatorServer() as simulator:
            print("Local Sim worker ready; only approved synthetic platform connections execute.", flush=True)
            while not (STATE / ".private/worker.stop").exists():
                try:
                    claim = await asyncio.to_thread(client.claim, ["preflight", "outcome_suite"])
                    if claim:
                        result = await execute(client, claim, simulator, profile, binding)
                        print(
                            json.dumps(
                                {
                                    "attempt": str(claim.attempt_id),
                                    "verdict": result.business_verdict,
                                    "execution": result.execution_state,
                                }
                            ),
                            flush=True,
                        )
                    else:
                        await asyncio.sleep(2)
                except Exception as error:
                    # Never replay or reuse a started assignment; retain its journal and wait for a new claim.
                    print("Claim refused or interrupted: " + type(error).__name__, flush=True)
                    await asyncio.sleep(3)
            print("Worker drained and stopped without interrupting an active assignment.", flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(main())
