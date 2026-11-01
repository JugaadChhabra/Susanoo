"""``susanoo-runner`` command line [Spec §18 › CI and scheduling, §27].

susanoo-runner local --suite examples/collections/suite.json --agent jsonl:correct
susanoo-runner local --suite suite.json --agent-cmd python -m my_agent.serve
susanoo-runner recover --journal .susanoo/journal.sqlite

CI commands on local runs (run directories under ``--runs-dir``, default ``.susanoo/runs``):

susanoo-runner preflight --suite suite.json --agent jsonl:correct
susanoo-runner run --suite suite.json --agent jsonl:correct [--detach] [--waiver-policy waivers.json]
susanoo-runner wait --run <handle> --timeout 900
susanoo-runner reproduce --run <handle> --case C01
susanoo-runner compare --baseline <handle> --candidate <handle>
susanoo-runner export --run <handle> --format all --output ./exports

Exit codes (run, wait, reproduce, compare, preflight): 0 ready under the configured gate; 1 observed
required failure; 2 operational error, unknown, incomplete or cancelled; 3 invalid configuration or auth.
Waivers apply only when ``--waiver-policy`` is passed and change acceptance, never the machine verdict.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
from pydantic import ValidationError

from susanoo_sdk import InProcessAgentAdapter, JsonlAgentProcess

from .ci import EXIT_CONFIG, EXIT_OPERATIONAL, ConfigError
from .journal import Journal, JournalError
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
    raise ConfigError(f"Unknown agent spec {spec!r}; use inprocess:<mode>, jsonl:<mode> or --agent-cmd")


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


class _Parser(argparse.ArgumentParser):
    """Usage errors are invalid configuration: exit 3, not argparse's default 2 [Spec §18]."""

    def error(self, message: str):  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(EXIT_CONFIG, f"{self.prog}: error: {message}\n")


def _add_agent(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--agent", default="jsonl:correct", help="inprocess:<mode> or jsonl:<mode> (demo agent)")
    parser.add_argument("--agent-cmd", nargs=argparse.REMAINDER, help="any JSONL agent command; must come last")


def _add_runs_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--runs-dir", type=Path, default=Path(".susanoo/runs"))


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="susanoo-runner")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    local = sub.add_parser("local", help="Run a suite locally against the simulator")
    local.add_argument("--suite", type=Path, required=True)
    local.add_argument("--journal", type=Path, default=Path(".susanoo/journal.sqlite"))
    local.add_argument("--report", type=Path)
    local.add_argument("--audience", choices=["operator", "engineer"], default="engineer")
    local.add_argument("--case", action="append", dest="cases")
    local.add_argument("--invocation-timeout", type=float)
    local.add_argument("--observer-outage", action="store_true", help="test switch: observer unavailable")
    local.add_argument("--cleanup-failure", action="store_true", help="test switch: cleanup fails")
    _add_agent(local)
    fidelity = sub.add_parser("fidelity-demo", help="Synthetic simulator-vs-reference discrepancy to repair")
    fidelity.add_argument("--report", type=Path)
    recover = sub.add_parser("recover", help="Mark interrupted dispatches uncertain after a crash")
    recover.add_argument("--journal", type=Path, default=Path(".susanoo/journal.sqlite"))

    platform = sub.add_parser("platform-worker", help="Execute locally approved platform claims; never remote commands")
    platform.add_argument("--url", required=True, help="Control plane URL ending in /api/v1")
    platform.add_argument("--token-env", default="SUSANOO_RUNNER_TOKEN")
    platform.add_argument("--profile", type=Path, required=True)
    platform.add_argument("--simulator-url", required=True)
    platform.add_argument("--simulator-key-env", default="SUSANOO_SIMULATOR_ADMIN_KEY")
    platform.add_argument("--state-dir", type=Path, default=Path(".susanoo/platform"))
    platform.add_argument("--once", action="store_true")
    _add_agent(platform)

    reconcile = sub.add_parser("platform-reconcile", help="Upload a stored journal without resuming execution")
    reconcile.add_argument("--url", required=True)
    reconcile.add_argument("--token-env", default="SUSANOO_RUNNER_TOKEN")
    reconcile.add_argument("--journal", type=Path, required=True)
    reconcile.add_argument("--attempt", required=True)

    preflight = sub.add_parser("preflight", help="Validate suite, profile and agent without running cases")
    preflight.add_argument("--suite", type=Path, required=True)
    preflight.add_argument("--profile", type=Path, help="LocalProfile JSON; the ceiling for this machine")
    preflight.add_argument("--case", action="append", dest="cases")
    preflight.add_argument("--timeout", type=float, default=20)
    _add_agent(preflight)

    run = sub.add_parser("run", help="Run an approved suite into a run directory; exit code enforces the gate")
    run.add_argument("--suite", type=Path, required=True)
    run.add_argument("--profile", type=Path)
    run.add_argument("--case", action="append", dest="cases")
    run.add_argument("--waiver-policy", type=Path, help="separately named acceptance-waiver policy (opt-in)")
    run.add_argument("--detach", action="store_true", help="start in the background; use `wait` for the result")
    run.add_argument("--run-id", help=argparse.SUPPRESS)
    run.add_argument("--invocation-timeout", type=float)
    run.add_argument("--observer-outage", action="store_true", help="test switch: observer unavailable")
    run.add_argument("--cleanup-failure", action="store_true", help="test switch: cleanup fails")
    run.add_argument("--json", action="store_true", help="print the acceptance decision as JSON")
    _add_runs_dir(run)
    _add_agent(run)

    wait = sub.add_parser("wait", help="Wait for a run to settle and exit with its gate code")
    wait.add_argument("--run", required=True)
    wait.add_argument("--timeout", type=float, default=900)
    wait.add_argument("--waiver-policy", type=Path)
    _add_runs_dir(wait)

    reproduce = sub.add_parser("reproduce", help="Rerun one case of a run in a fresh fixture namespace")
    reproduce.add_argument("--run", required=True)
    reproduce.add_argument("--case", required=True)
    _add_runs_dir(reproduce)

    compare = sub.add_parser("compare", help="Regression transitions between two runs")
    compare.add_argument("--baseline", required=True)
    compare.add_argument("--candidate", required=True)
    compare.add_argument("--audience", choices=["operator", "engineer"], default="engineer")
    compare.add_argument("--output", type=Path)
    _add_runs_dir(compare)

    export = sub.add_parser("export", help="JSON, JUnit XML and reproduction ingredients from one snapshot")
    export.add_argument("--run", required=True)
    export.add_argument("--format", choices=["json", "junit", "bundle", "all"], default="json")
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--audience", choices=["operator", "engineer"], default="engineer")
    _add_runs_dir(export)
    return parser


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(raw)
    try:
        return _dispatch(args, raw)
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return EXIT_CONFIG
    except JournalError as error:
        print(f"Runner stopped ({error.code}); no business action was automatically retried.", file=sys.stderr)
        return EXIT_OPERATIONAL
    except ValidationError:
        print("Configuration or claim validation failed; sensitive input values are not logged.", file=sys.stderr)
        return EXIT_CONFIG
    except httpx.HTTPError:
        print(
            "Platform transport failed; preserve the journal and reconcile before further execution.", file=sys.stderr
        )
        return EXIT_OPERATIONAL


def _dispatch(args: argparse.Namespace, raw: list[str]) -> int:
    if args.command == "platform-worker":
        return _platform_worker(args)
    if args.command == "platform-reconcile":
        from .platform import PlatformClient, reconcile_stored

        token = os.environ.get(args.token_env)
        if not token:
            raise ConfigError(f"Set {args.token_env} locally; do not put bearer tokens in command arguments")
        client = PlatformClient(args.url, token)
        try:
            print(json.dumps(reconcile_stored(client, args.journal, args.attempt)))
            return EXIT_OPERATIONAL  # late evidence is not a new successful execution
        finally:
            client.close()
    if args.command == "recover":
        journal = Journal(args.journal)
        uncertain = journal.recover()
        print(f"{len(uncertain)} step(s) may have run before the crash and need read-back reconciliation.")
        for step in uncertain:
            print(f"  attempt {step.attempt_id} step {step.step_id} ({step.action_kind})")
        return 0
    if args.command == "fidelity-demo":
        return asyncio.run(fidelity_demo(args.report))
    if args.command == "local":
        return _local(args)
    handler = {
        "preflight": _preflight,
        "run": _run,
        "wait": _wait,
        "reproduce": _reproduce,
        "compare": _compare,
        "export": _export,
    }[args.command]
    return handler(args, raw) if args.command == "run" else handler(args)


def _platform_worker(args):
    from susanoo_simulator.adapter import SimulatorFixtureAdapter
    from .platform import PlatformClient, PlatformProfile, execute_claim
    from .local_security import secure_local_path

    token, key = os.environ.get(args.token_env), os.environ.get(args.simulator_key_env)
    if not token or not key:
        raise ConfigError("Runner token and simulator key must be set in the named environment variables")
    profile = PlatformProfile.model_validate_json(args.profile.read_text(encoding="utf-8"))
    factory, _ = agent_factory(args.agent, args.agent_cmd)
    marker = args.state_dir / ".susanoo-runner-owned"
    if args.state_dir.is_symlink() or (
        args.state_dir.exists() and any(args.state_dir.iterdir()) and not marker.is_file()
    ):
        raise ConfigError("Use a dedicated empty runner state directory, not an existing project/data directory")
    args.state_dir.mkdir(parents=True, exist_ok=True)
    secure_local_path(args.state_dir)
    marker.touch(exist_ok=True)
    client = PlatformClient(args.url, token)

    async def perform(claim):
        fixtures = SimulatorFixtureAdapter(args.simulator_url, key)
        try:
            return await execute_claim(
                client,
                claim,
                profile=profile,
                journal_path=args.state_dir / f"{claim.attempt_id}.sqlite",
                agent_factory=factory,
                fixtures=fixtures,
            )
        finally:
            await fixtures.aclose()

    try:
        while True:
            claim = client.claim()
            if claim:
                result = asyncio.run(perform(claim))
                print(f"{result.attempt_id}: {result.execution_state} / {result.business_verdict}")
                if args.once:
                    return _platform_exit_code(result)
            elif args.once:
                return EXIT_OPERATIONAL
            else:
                time.sleep(2)
    except KeyboardInterrupt:
        return EXIT_OPERATIONAL
    finally:
        client.close()


def _platform_exit_code(result):
    if result.execution_state == "completed" and result.business_verdict == "FAIL":
        return 1
    healthy = (
        result.execution_state == "completed"
        and result.business_verdict == "PASS"
        and result.cleanup_state in {"succeeded", "not_required"}
        and result.reconciliation_state in {"not_needed", "resolved"}
    )
    return 0 if healthy else EXIT_OPERATIONAL


def _local(args: argparse.Namespace) -> int:
    from .ci import load_suite

    suite = load_suite(args.suite)
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


def _profile(args: argparse.Namespace) -> LocalProfile:
    from .ci import load_profile

    profile = load_profile(getattr(args, "profile", None))
    if getattr(args, "invocation_timeout", None):
        profile = profile.model_copy(update={"invocation_timeout_seconds": args.invocation_timeout})
    return profile


def _preflight(args: argparse.Namespace) -> int:
    from susanoo_evaluator import evaluator_digest

    from .ci import load_suite, select_cases

    suite = load_suite(args.suite)
    profile = _profile(args)
    cases = select_cases(suite, profile, args.cases)
    factory, label = agent_factory(args.agent, args.agent_cmd)
    print(f"Suite {suite.id}: {len(cases)} case(s), {sum(c.required for c in cases)} required, checks valid")
    print(f"Local profile admits every selected case; evaluator {evaluator_digest()[:12]}")

    async def probe():
        adapter = factory()
        try:
            return await asyncio.wait_for(adapter.preflight(), args.timeout)
        finally:
            closer = getattr(adapter, "aclose", None)
            if closer:
                await closer()

    try:
        report = asyncio.run(probe())
    except Exception as error:
        print(f"Agent {label} did not answer preflight: {type(error).__name__}: {error}", file=sys.stderr)
        return EXIT_OPERATIONAL
    caps = report.capabilities
    print(f"Agent {label}: adapter {report.adapter} {report.adapter_version}, protocol {report.protocol_version}")
    if not caps.invoke:
        print("Agent does not declare invoke; it cannot be tested", file=sys.stderr)
        return EXIT_CONFIG
    print("Preflight passed")
    return 0


def _run(args: argparse.Namespace, raw: list[str]) -> int:
    from .ci import RunDir, evaluate_acceptance, execute_run, load_suite, load_waiver_policy, prepare_run

    policy = load_waiver_policy(args.waiver_policy)
    suite = load_suite(args.suite)
    profile = _profile(args)
    factory, label = agent_factory(args.agent, args.agent_cmd)
    run_dir = RunDir.create(args.runs_dir, args.run_id)
    if args.detach:
        from .ci import RunStatus

        prepare_run(
            run_dir,
            suite,
            agent_label=label,
            agent_spec=args.agent,
            agent_cmd=args.agent_cmd,
            profile=profile,
            case_ids=args.cases,
        )
        run_dir.write_status(RunStatus(state="queued"))
        child = _without_detach(raw) + ["--run-id", run_dir.handle]
        if args.agent_cmd:  # --run-id must precede the agent command remainder
            cut = child.index("--agent-cmd")
            child = child[:cut] + child[-2:] + child[cut:-2]
        with open(run_dir.log_path, "ab") as log:
            subprocess.Popen(
                [sys.executable, "-m", "susanoo_runner.cli", *child],
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env={**os.environ, "PYTHONPATH": _pythonpath()},
                start_new_session=True,
            )
        print(f"Run {run_dir.handle} started in the background ({run_dir.path})")
        print(f"Wait for it: susanoo-runner wait --run {run_dir.handle} --runs-dir {args.runs_dir}")
        return 0
    if not run_dir.manifest_path.exists():
        prepare_run(
            run_dir,
            suite,
            agent_label=label,
            agent_spec=args.agent,
            agent_cmd=args.agent_cmd,
            profile=profile,
            case_ids=args.cases,
        )
    try:
        report = execute_run(
            run_dir,
            run_dir.suite(),
            agent_factory=factory,
            profile=profile,
            observer_outage=args.observer_outage,
            cleanup_failure=args.cleanup_failure,
        )
    except KeyboardInterrupt:
        print("Run cancelled", file=sys.stderr)
        return EXIT_OPERATIONAL
    except Exception as error:
        print(f"Run did not finish: {type(error).__name__}: {error}", file=sys.stderr)
        return EXIT_OPERATIONAL
    print_summary(report)
    return _announce(run_dir.handle, evaluate_acceptance(report, policy), as_json=args.json)


def _without_detach(raw: list[str]) -> list[str]:
    stop = raw.index("--agent-cmd") if "--agent-cmd" in raw else len(raw)
    return [item for i, item in enumerate(raw) if not (i < stop and item == "--detach")]


def _announce(handle: str, acceptance, as_json: bool = False) -> int:
    if as_json:
        print(json.dumps({"run": handle, **acceptance.model_dump(mode="json")}))
    else:
        print(f"Run {handle}: machine gate {acceptance.machine_gate}, acceptance {acceptance.decision}")
        for item in acceptance.applied:
            print(f"  waiver {item['waiver']} accepted {item['case_id']} ({item['verdict']} remains in the record)")
        for waiver_id in acceptance.expired:
            print(f"  waiver {waiver_id} has expired and was not applied")
        print(f"Exit code {acceptance.exit_code}")
    return acceptance.exit_code


def _wait(args: argparse.Namespace) -> int:
    from .ci import evaluate_acceptance, load_waiver_policy, resolve_run

    policy = load_waiver_policy(args.waiver_policy)
    run_dir = resolve_run(args.run, args.runs_dir)
    deadline = time.monotonic() + args.timeout
    while True:
        status = run_dir.status()
        if status.state == "finished":
            report = run_dir.report()
            print_summary(report)
            return _announce(run_dir.handle, evaluate_acceptance(report, policy))
        if status.state == "failed":
            print(f"Run {run_dir.handle} failed: {status.message}", file=sys.stderr)
            return status.exit_code if status.exit_code is not None else EXIT_OPERATIONAL
        if status.state == "running" and status.pid and not _alive(status.pid):
            print(f"Run {run_dir.handle} was interrupted (runner process {status.pid} is gone)", file=sys.stderr)
            return EXIT_OPERATIONAL
        if time.monotonic() >= deadline:
            print(f"Run {run_dir.handle} is still {status.state} after {args.timeout:.0f}s (incomplete)")
            return EXIT_OPERATIONAL
        time.sleep(0.2)


def _alive(pid: int) -> bool:
    if os.name == "nt":
        import psutil

        return psutil.pid_exists(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _reproduce(args: argparse.Namespace) -> int:
    from .ci import RunDir, evaluate_acceptance, execute_run, prepare_run, reproduction_outcome, resolve_run

    original_dir = resolve_run(args.run, args.runs_dir)
    manifest, report = original_dir.manifest(), original_dir.report()
    original = [a for a in report.attempts if a.case_id == args.case]
    if not original:
        raise ConfigError(f"Run {original_dir.handle} has no attempt for case {args.case}")
    if manifest.agent_cmd and "[REDACTED]" in " ".join(manifest.agent_cmd):
        raise ConfigError("The recorded agent command had secrets redacted; rerun with `run --agent-cmd` instead")
    suite = original_dir.suite()
    if suite.content_hash != manifest.suite_hash:
        raise ConfigError("The frozen suite no longer matches the run manifest; refusing to reproduce")
    profile = LocalProfile.model_validate(manifest.profile)
    factory, _ = agent_factory(manifest.agent_spec, manifest.agent_cmd)
    run_dir = RunDir.create(args.runs_dir)
    prepare_run(
        run_dir,
        suite,
        agent_label=manifest.agent_label,
        agent_spec=manifest.agent_spec,
        agent_cmd=manifest.agent_cmd,
        profile=profile,
        case_ids=[args.case],
        reproduces={"handle": original_dir.handle, "case_id": args.case},
    )
    fresh_report = execute_run(run_dir, suite, agent_factory=factory, profile=profile)
    fresh = fresh_report.attempts[0]
    outcome = reproduction_outcome(original[-1], fresh)
    print(
        f"Reproduction of {args.case} from {original_dir.handle}: {outcome} "
        f"(original {original[-1].business_verdict}, fresh {fresh.business_verdict} in namespace {fresh.namespace})"
    )
    print(f"Fresh run {run_dir.handle}; the original evidence is untouched")
    return _announce(run_dir.handle, evaluate_acceptance(fresh_report, None))


def _compare(args: argparse.Namespace) -> int:
    from .ci import resolve_run, write_json
    from .compare import compare_runs, comparison_exit_code

    base, cand = resolve_run(args.baseline, args.runs_dir), resolve_run(args.candidate, args.runs_dir)
    comparison = compare_runs(base.report(), base.manifest(), cand.report(), cand.manifest(), args.audience)
    for row in comparison.rows:
        line = f"  {row.case_id:<6} {row.category:<15} {row.transition or '-':<14}"
        if row.reasons:
            line += f" not comparable: {', '.join(row.reasons)}"
        if row.check_transitions:
            line += " [" + ", ".join(f"{t.check_id} {t.transition}" for t in row.check_transitions) + "]"
        print(line)
    print("Summary: " + ", ".join(f"{k} {v}" for k, v in comparison.summary.items()))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        write_json(args.output, comparison.model_dump(mode="json"))
    return comparison_exit_code(comparison)


def _export(args: argparse.Namespace) -> int:
    from .ci import resolve_run
    from .export import write_exports

    run_dir = resolve_run(args.run, args.runs_dir)
    formats = ["json", "junit", "bundle"] if args.format == "all" else [args.format]
    written = write_exports(run_dir, formats, args.output, args.audience)
    for fmt, path in written.items():
        print(f"{fmt}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
