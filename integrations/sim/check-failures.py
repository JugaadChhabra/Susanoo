"""Focused fail-closed checks; never calls real customer endpoints."""

import asyncio
import json
from sim_agent import SimAgent
from susanoo_runner.journal import Journal
from susanoo_runner.local import SimulatorServer, run_suite
from susanoo_runner.suite import LocalSuite, LocalProfile
from susanoo_sdk import InProcessAgentAdapter

from paths import REPO, STATE, PRIVATE

raw = json.loads((REPO / "examples/collections/suite.json").read_text(encoding="utf-8"))
raw["id"] = "sim-transport-failure-one-case-suite"
raw["cases"] = raw["cases"][:1]
suite = LocalSuite.model_validate(raw)
profile = LocalProfile(allowed_operations=raw["cases"][0]["fixture"]["recipe"]["seed"]["actors"]["agent"]["operations"])


async def main():
    try:
        SimAgent(binding_pin="0" * 64)
    except PermissionError:
        print("PASS: mismatched local binding refused before dispatch")
    else:
        raise AssertionError("Unapproved binding accepted")
    results = []
    with SimulatorServer() as simulator:
        for failure in ["invalid_key", "missing_version"]:
            agent = SimAgent()
            if failure == "invalid_key":
                agent.binding["api_key"] = "deliberately-invalid-local-test-key"
            else:
                agent.binding["deployment_version"] = 999999999
            journal = Journal(PRIVATE / f"failure-{failure}.sqlite3")
            try:
                report = await run_suite(
                    suite,
                    agent_factory=lambda agent=agent: InProcessAgentAdapter(agent),
                    agent_label="Sim local negative transport control",
                    journal=journal,
                    simulator_url=simulator.base_url,
                    simulator_admin_key=simulator.admin_key,
                    profile=profile,
                )
                result = {
                    "failure": failure,
                    "gate": report.gate,
                    "execution": report.attempts[0].execution_state,
                    "tool_calls": len(agent.tool_results),
                }
                assert report.gate == "BLOCKED" and result["execution"] == "error" and not agent.tool_results
                results.append(result)
                print("PASS: " + json.dumps(result))
            finally:
                journal.close()
    (STATE / "failure-report.json").write_text(json.dumps(results, indent=2), encoding="utf-8")


asyncio.run(main())
