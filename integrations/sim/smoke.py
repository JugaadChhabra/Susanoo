"""Run a real local Sim workflow through Susanoo; all business data is synthetic."""

import asyncio
import json

from susanoo_runner.journal import Journal
from susanoo_runner.local import SimulatorServer, run_suite
from susanoo_runner.suite import LocalProfile, LocalSuite
from susanoo_sdk import InProcessAgentAdapter
from sim_agent import SimAgent as SimWorkflow

from paths import REPO, STATE, PRIVATE

binding = json.loads((STATE / ".private/binding.json").read_text())


async def main():
    raw = json.loads((REPO / "examples/collections/suite.json").read_text(encoding="utf-8"))
    raw["id"] = "sim-local-one-case-smoke-v1"
    raw["cases"] = [raw["cases"][0]]
    raw["approval"] = {
        "kind": "synthetic_demo",
        "note": "Standalone one-case smoke suite, not the twenty-case release suite",
    }
    suite = LocalSuite.model_validate(raw)
    summary = {
        "scope": "Local Sim engine + real Susanoo runner/evaluator + synthetic simulator only",
        "not_proven": [
            "V3 qualification",
            "live provider or customer agent",
            "twenty-case suite",
            "platform claim/upload bridge",
        ],
        "workflow_id": binding["workflow_id"],
        "results": [],
    }
    with SimulatorServer() as server:
        for mode, outage, expected in [
            ("correct", False, "PASS"),
            ("false_completion", False, "FAIL"),
            ("wrong_amount", False, "FAIL"),
            ("correct", True, "UNKNOWN"),
        ]:
            agent = SimWorkflow(mode)
            journal = Journal(PRIVATE / f"journal-{mode}-{outage}.sqlite3")
            try:
                report = await run_suite(
                    suite,
                    agent_factory=lambda agent=agent: InProcessAgentAdapter(agent),
                    agent_label=f"sim:{binding['workflow_id']}:{mode}",
                    journal=journal,
                    simulator_url=server.base_url,
                    simulator_admin_key=server.admin_key,
                    profile=LocalProfile(
                        allowed_operations=[
                            "read_account",
                            "list_promises",
                            "create_promise",
                            "request_handoff",
                            "adjust_balance",
                            "waive_fees",
                        ]
                    ),
                    observer_outage=outage,
                )
                attempt = report.attempts[0]
                result = {
                    "mode": mode,
                    "observer_outage": outage,
                    "expected": expected,
                    "actual": attempt.business_verdict,
                    "execution": attempt.execution_state,
                    "sim_run_ids": agent.runs,
                    "tool_results": agent.tool_results,
                    "gate": report.gate,
                }
                summary["results"].append(result)
                (STATE / "smoke-report.json").write_text(json.dumps(summary, indent=2))
                print(json.dumps(result))
                if attempt.business_verdict != expected or not agent.runs:
                    raise AssertionError("Local Sim/Susanoo boundary did not produce the expected verdict")
            finally:
                journal.close()
    print("PASS: correct / false completion / wrong amount / unavailable observer")


if __name__ == "__main__":
    asyncio.run(main())
