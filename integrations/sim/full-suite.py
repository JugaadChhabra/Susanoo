"""Twenty-case local regression through a real JSONL process and Sim HTTP engine."""

import asyncio
import json
import sys
from susanoo_runner.local import SimulatorServer, run_suite
from susanoo_runner.journal import Journal
from susanoo_runner.suite import LocalSuite, LocalProfile
from susanoo_sdk import JsonlAgentProcess

from paths import ROOT, REPO, STATE

suite = LocalSuite.model_validate_json((REPO / "examples/collections/suite.json").read_text(encoding="utf-8"))


async def main():
    journal = Journal(STATE / ".private/full-suite.sqlite3")
    try:
        with SimulatorServer() as server:
            report = await run_suite(
                suite,
                agent_factory=lambda: JsonlAgentProcess(
                    [sys.executable, str(ROOT / "serve.py")], cwd=str(ROOT), env={"SUSANOO_SIM_STATE_DIR": str(STATE)}
                ),
                agent_label="Sim local synthetic workflow via JSONL",
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
            )
            (STATE / ".private/full-suite-report.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
            summary = {
                "scope": "Real local Sim, JSONL, runner, evaluator and synthetic backend; not live qualification",
                "gate": report.gate,
                "counts": report.counts.model_dump(),
                "cases": [
                    {"id": a.case_id, "verdict": a.business_verdict, "execution": a.execution_state}
                    for a in report.attempts
                ],
            }
            (STATE / "full-suite-summary.json").write_text(json.dumps(summary, indent=2))
            print(json.dumps(summary, indent=2))
            if len(report.attempts) != 20 or report.counts.passed != 20 or report.gate != "READY_FOR_ACCEPTANCE":
                raise AssertionError("Twenty-case local integration is not ready")
    finally:
        journal.close()


asyncio.run(main())
