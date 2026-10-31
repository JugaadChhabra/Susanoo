"""``susanoo-runner`` command line.

susanoo-runner local --suite examples/collections/suite.json --agent jsonl:correct
susanoo-runner local --suite suite.json --agent-cmd python -m my_agent.serve
susanoo-runner recover --journal .susanoo/journal.sqlite
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from susanoo_sdk import InProcessAgentAdapter, JsonlAgentProcess

from .journal import Journal
from .local import RunReport, SimulatorServer, run_suite_sync
from .suite import LocalProfile, LocalSuite


def _pythonpath() -> str:
    return os.pathsep.join(path for path in sys.path if path)


def agent_factory(spec: str | None, command: list[str] | None):
    """``inprocess:<mode>`` and ``jsonl:<mode>`` drive the demo agent; ``--agent-cmd`` drives any JSONL agent."""
    if command:
        return (lambda: JsonlAgentProcess(command, name="custom")), "jsonl:" + " ".join(command)
    kind, _, mode = (spec or "jsonl:correct").partition(":")
    mode = mode or "correct"
    if kind == "inprocess":
        from collections_demo import CollectionsAgent

        return (lambda: InProcessAgentAdapter(CollectionsAgent(mode))), f"inprocess:{mode}"
    if kind == "jsonl":
        argv = [sys.executable, "-m", "collections_demo.serve", "--mode", mode]
        env = {"PYTHONPATH": _pythonpath()}
        return (lambda: JsonlAgentProcess(argv, env=env, name=f"demo-{mode}")), f"jsonl:{mode}"
    raise SystemExit(f"Unknown agent spec {spec}")


def print_summary(report: RunReport) -> None:
    c = report.counts
    print(f"Suite {report.suite_id} ({'synthetic' if report.synthetic else 'customer'}) with agent {report.agent}")
    print(f"Gate: {report.gate}")
    print(
        f"{c.scheduled} scheduled, {c.completed} completed, {c.passed} passed, {c.failed} failed, "
        f"{c.unknown} unknown, {c.execution_errors} execution error(s)"
    )
    for attempt in report.attempts:
        title = "Held-out case" if attempt.visibility == "hidden" else attempt.title
        line = f"  {attempt.case_id} {attempt.business_verdict:<7} {attempt.execution_state:<10} {title}"
        if attempt.visibility != "hidden":
            failing = [o.check_id for o in attempt.outcomes if o.verdict != "PASS"]
            if failing:
                line += f"  [{', '.join(failing)}]"
        print(line)
    for blocker in report.blockers[:20]:
        print(f"  blocker: {blocker}")


async def fidelity_demo(report_path: Path | None) -> int:
    """Discrepancy -> task impact -> reduced reproduction -> repair gate, all synthetic and local."""
    from collections_demo import CollectionsAgent, build_suite
    from collections_demo import fidelity as demo
    from susanoo_simulator.environment import SimulatorEnvironment

    from .fidelity import compare
    from .impact import ImpactRunner
    from .reduce import reduce_reproduction
    from .repair import validate_simulator_repair
    from .suite import Limits

    cases = {case.id: case for case in LocalSuite.model_validate(build_suite()).cases}
    contract = demo.contract()
    out: dict = {"synthetic": True, "contract": contract.id}
    with SimulatorServer() as server:
        sim = SimulatorEnvironment(server.base_url, server.admin_key, label="simulator")
        ref = SimulatorEnvironment(server.base_url, server.admin_key, label="reference")
        try:
            comparison = await compare(contract, demo.SESSION, sim, ref)
            print(
                f"1. Compared {len(demo.SESSION.actions)} actions: "
                f"{len(comparison.candidates)} discrepancy candidate(s)"
            )
            for candidate in comparison.candidates:
                print(f"   - [{candidate.category}] {candidate.summary} (unresolved; owner classifies)")
            journal = Journal(Path(tempfile_dir()) / "impact.sqlite")
            impacts = []
            for mode, case_id in (("duplicate", "C13"), ("forbidden_change", "C01"), ("correct", "C01")):
                runner = ImpactRunner(
                    journal,
                    simulator_url=server.base_url,
                    admin_key=server.admin_key,
                    limits=Limits(),
                    agent_factory=lambda mode=mode: InProcessAgentAdapter(CollectionsAgent(mode)),
                )
                try:
                    pair = await runner.pair(cases[case_id], demo.SIMULATOR, demo.REFERENCE)
                finally:
                    await runner.aclose()
                impacts.append(pair)
                print(f"2. Task impact, {mode} agent on {case_id}: {pair.relationship}, attribution {pair.attribution}")
            reduction = await reduce_reproduction(contract, demo.SESSION, demo.duplicate_predicate, sim, ref)
            print(
                f"3. Reduced the duplicate-write reproduction from {len(demo.SESSION.actions)} to "
                f"{len(reduction.reduced_plan.actions)} action(s) in {reduction.runs_used} run(s) ({reduction.label})"
            )
            validation = await validate_simulator_repair(
                contract,
                demo.repair_proposal(),
                reduction,
                demo.duplicate_predicate,
                controls=demo.CONTROLS,
                heldout=demo.HELDOUT,
                sim_env=sim,
                ref_env=ref,
            )
            print(f"4. Simulator repair: {validation.outcome}")
            for check in validation.checks:
                print(f"   - {check.name}: {'pass' if check.passed else 'FAIL'} ({check.detail})")
        finally:
            await sim.aclose()
            await ref.aclose()
    out.update(
        comparison=comparison.model_dump(mode="json"),
        impact=[pair.model_dump(mode="json") for pair in impacts],
        reduction=reduction.model_dump(mode="json"),
        repair=validation.model_dump(mode="json"),
    )
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return 0 if validation.outcome == "accepted" else 1


def tempfile_dir() -> str:
    import tempfile

    return tempfile.mkdtemp(prefix="susanoo-impact-")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="susanoo-runner")
    sub = parser.add_subparsers(dest="command", required=True)
    local = sub.add_parser("local", help="Run a suite locally against the simulator")
    local.add_argument("--suite", type=Path, required=True)
    local.add_argument("--agent", default="jsonl:correct")
    local.add_argument("--agent-cmd", nargs=argparse.REMAINDER)
    local.add_argument("--journal", type=Path, default=Path(".susanoo/journal.sqlite"))
    local.add_argument("--report", type=Path)
    local.add_argument("--audience", choices=["operator", "engineer"], default="engineer")
    local.add_argument("--case", action="append", dest="cases")
    local.add_argument("--invocation-timeout", type=float)
    local.add_argument("--observer-outage", action="store_true", help="test switch: observer unavailable")
    local.add_argument("--cleanup-failure", action="store_true", help="test switch: cleanup fails")
    fidelity = sub.add_parser("fidelity-demo", help="Synthetic simulator-vs-reference discrepancy to repair")
    fidelity.add_argument("--report", type=Path)
    recover = sub.add_parser("recover", help="Mark interrupted dispatches uncertain after a crash")
    recover.add_argument("--journal", type=Path, default=Path(".susanoo/journal.sqlite"))
    args = parser.parse_args(argv)

    if args.command == "recover":
        journal = Journal(args.journal)
        uncertain = journal.recover()
        print(f"{len(uncertain)} step(s) may have run before the crash and need read-back reconciliation.")
        for step in uncertain:
            print(f"  attempt {step.attempt_id} step {step.step_id} ({step.action_kind})")
        return 0

    if args.command == "fidelity-demo":
        return asyncio.run(fidelity_demo(args.report))

    suite = LocalSuite.model_validate_json(args.suite.read_text(encoding="utf-8"))
    factory, label = agent_factory(args.agent, args.agent_cmd)
    profile = LocalProfile()
    if args.invocation_timeout:
        profile = profile.model_copy(update={"invocation_timeout_seconds": args.invocation_timeout})
    journal = Journal(args.journal)
    with SimulatorServer() as simulator:
        report = run_suite_sync(
            suite,
            agent_factory=factory,
            agent_label=label,
            journal=journal,
            simulator_url=simulator.base_url,
            simulator_admin_key=simulator.admin_key,
            profile=profile,
            observer_outage=args.observer_outage,
            cleanup_failure=args.cleanup_failure,
            case_ids=args.cases,
        )
    journal.close()
    print_summary(report)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report.view(args.audience), indent=2, ensure_ascii=False), encoding="utf-8")
    return 0 if report.gate == "READY_FOR_ACCEPTANCE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
