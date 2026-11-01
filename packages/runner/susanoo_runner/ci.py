"""CI semantics for local runs: run directories, exit codes and acceptance waivers [Spec §13, §16, §18; T16].

A local run lives in one directory and is never rewritten after it finishes::

    <runs-dir>/<handle>/
        suite.json      frozen copy of the approved suite the run executed (immutable input snapshot)
        manifest.json   per-case digests (case, inputs, fixture, checks), agent/adapter identity, lineage
        status.json     queued | running | finished | failed, with pid and machine exit code
        report.json     the full ``RunReport`` (local operator view; exports choose the audience)
        journal.sqlite  the durable journal for this run (observed dependencies come from here)

Exit codes [Spec §18 › CI and scheduling]: 0 ready under the configured gate; 1 observed required
failure; 2 operational error, unknown, incomplete or cancelled; 3 invalid configuration or auth.
Exit 1 means a required FAIL on an attempt whose execution completed: the agent ran and the evidence
proves the violation. It wins over execution errors in other cases, which stay visible in the report
and JUnit export. A FAIL only on attempts that errored (e.g. the agent never started, so nothing was
written) is incomplete evidence about the agent and exits 2.

Waivers live in a separately named policy file (``kind: susanoo.acceptance_waivers``). They change the
acceptance decision only, never the machine verdict or gate, and are applied only when the policy file
is passed explicitly. Operational blockers (execution errors, cleanup failure, unresolved side effects)
cannot be waived. Expired waivers never authorize acceptance.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from susanoo_contracts import content_hash

from .local import RunReport
from .suite import LocalCase, LocalProfile, LocalSuite

EXIT_READY = 0
EXIT_REQUIRED_FAILURE = 1
EXIT_OPERATIONAL = 2
EXIT_CONFIG = 3

WAIVER_POLICY_KIND = "susanoo.acceptance_waivers"


class ConfigError(Exception):
    """Invalid configuration or authentication: always exit code 3."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- manifest -----------------------------------------------------------------------------------


class CaseManifest(_Model):
    case_id: str
    title: str
    visibility: Literal["public", "hidden"]
    required: bool
    requirement_ids: list[str]
    case_digest: str = Field(description="the whole case, including checks and fixture")
    inputs_digest: str = Field(description="script, observations, scope flags; excludes checks and fixture")
    fixture_digest: str
    fixture_adapter: str
    fixture_compatibility_key: str | None = None
    check_digests: dict[str, str]
    required_checks: list[str]


def case_manifest(case: LocalCase) -> CaseManifest:
    data = case.model_dump(mode="json")
    inputs = {key: value for key, value in data.items() if key not in {"checks", "fixture", "title"}}
    return CaseManifest(
        case_id=case.id,
        title=case.title,
        visibility=case.visibility,
        required=case.required,
        requirement_ids=list(case.requirement_ids),
        case_digest=content_hash(data),
        inputs_digest=content_hash(inputs),
        fixture_digest=case.fixture.digest,
        fixture_adapter=case.fixture.adapter,
        fixture_compatibility_key=getattr(case.fixture, "compatibility_key", None),
        check_digests={check["id"]: content_hash(check) for check in data["checks"]},
        required_checks=[check.id for check in case.parsed_checks() if check.required],
    )


class RunManifest(_Model):
    schema_version: Literal["1.0"] = "1.0"
    handle: str
    run_id: str | None = None
    suite_id: str
    suite_hash: str
    synthetic: bool
    agent_label: str
    agent_spec: str | None = None
    agent_cmd: list[str] | None = None
    agent_adapter: str
    evaluator_digest: str
    created_at: AwareDatetime
    case_filter: list[str] | None = None
    profile: dict[str, Any]
    cases: list[CaseManifest]
    reproduces: dict[str, str] | None = Field(default=None, description="{'handle': ..., 'case_id': ...}")

    def case(self, case_id: str) -> CaseManifest | None:
        return next((item for item in self.cases if item.case_id == case_id), None)


class RunStatus(_Model):
    state: Literal["queued", "running", "finished", "failed"]
    pid: int | None = None
    exit_code: int | None = None
    message: str = ""
    updated_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))


_SECRET_ARG = re.compile(r"(?i)(token|secret|password|passwd|api[_-]?key|authorization)(=|:)\S+")


def redact_argv(argv: list[str] | None) -> list[str] | None:
    """Agent commands are recorded for reproduction; inline secrets are not."""
    if argv is None:
        return None
    out, hide_next = [], False
    for item in argv:
        if hide_next:
            out.append("[REDACTED]")
            hide_next = False
            continue
        if re.fullmatch(r"(?i)--?(token|secret|password|api[_-]?key)", item):
            hide_next = True
            out.append(item)
            continue
        out.append(_SECRET_ARG.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", item))
    return out


def write_json(path: Path, data: Any) -> None:
    """Atomic replace so a concurrent ``wait`` never reads a torn file."""
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, path)


class RunDir:
    def __init__(self, path: Path):
        self.path = Path(path)

    @property
    def handle(self) -> str:
        return self.path.name

    suite_path = property(lambda self: self.path / "suite.json")
    manifest_path = property(lambda self: self.path / "manifest.json")
    status_path = property(lambda self: self.path / "status.json")
    report_path = property(lambda self: self.path / "report.json")
    journal_path = property(lambda self: self.path / "journal.sqlite")
    log_path = property(lambda self: self.path / "run.log")

    @classmethod
    def create(cls, runs_dir: Path, handle: str | None = None) -> RunDir:
        handle = handle or f"lr-{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
        path = Path(runs_dir) / handle
        path.mkdir(parents=True, exist_ok=True)
        return cls(path)

    def write_status(self, status: RunStatus) -> None:
        write_json(self.status_path, status.model_dump(mode="json"))

    def status(self) -> RunStatus:
        try:
            return RunStatus.model_validate_json(self.status_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ConfigError(f"{self.path} is not a Susanoo run directory (no status.json)") from None

    def manifest(self) -> RunManifest:
        return RunManifest.model_validate_json(self.manifest_path.read_text(encoding="utf-8"))

    def write_manifest(self, manifest: RunManifest) -> None:
        write_json(self.manifest_path, manifest.model_dump(mode="json"))

    def report(self) -> RunReport:
        if not self.report_path.exists():
            raise ConfigError(f"Run {self.handle} has no report yet (state {self.status().state})")
        return RunReport.model_validate_json(self.report_path.read_text(encoding="utf-8"))

    def suite(self) -> LocalSuite:
        return LocalSuite.model_validate_json(self.suite_path.read_text(encoding="utf-8"))


def resolve_run(ref: str, runs_dir: Path) -> RunDir:
    """A run reference is a run directory path or a handle under ``runs_dir``."""
    for candidate in (Path(ref), Path(runs_dir) / ref):
        if (candidate / "status.json").exists():
            return RunDir(candidate)
    raise ConfigError(f"No local run {ref!r} (looked in {runs_dir})")


# --- configuration loading ----------------------------------------------------------------------


def load_suite(path: Path) -> LocalSuite:
    try:
        return LocalSuite.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"Suite file not found: {path}") from None
    except ValidationError as error:
        raise ConfigError(f"Suite {path} is invalid: {error.error_count()} problem(s); {_first(error)}") from None


def load_profile(path: Path | None) -> LocalProfile:
    if path is None:
        return LocalProfile()
    try:
        return LocalProfile.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"Profile file not found: {path}") from None
    except ValidationError as error:
        raise ConfigError(f"Profile {path} is invalid: {_first(error)}") from None


def select_cases(suite: LocalSuite, profile: LocalProfile, case_ids: list[str] | None) -> list[LocalCase]:
    """Every selected case must exist and fit the local profile before anything executes."""
    known = {case.id for case in suite.cases}
    unknown = sorted(set(case_ids or []) - known)
    if unknown:
        raise ConfigError(f"Unknown case id(s) for suite {suite.id}: {', '.join(unknown)}")
    selected = [case for case in suite.cases if not case_ids or case.id in case_ids]
    refusals = [f"{case.id}: {'; '.join(problems)}" for case in selected if (problems := profile.admit(case))]
    if refusals:
        raise ConfigError("The local profile refuses: " + " | ".join(refusals))
    return selected


def _first(error: ValidationError) -> str:
    item = error.errors()[0]
    return f"{'.'.join(str(p) for p in item['loc'])}: {item['msg']}"


# --- exit codes and acceptance ------------------------------------------------------------------


def machine_exit_code(report: RunReport) -> int:
    """Exit code from the machine gate alone; waivers never enter here."""
    if report.gate == "READY_FOR_ACCEPTANCE":
        return EXIT_READY
    if any(a.required and a.business_verdict == "FAIL" and a.execution_state == "completed" for a in report.attempts):
        return EXIT_REQUIRED_FAILURE
    return EXIT_OPERATIONAL


class WaiverScope(_Model):
    suite_id: str
    case_id: str
    check_ids: list[str] = Field(default_factory=list, description="empty means every required check of the case")
    agent: str | None = Field(default=None, description="restrict to one agent label")


class Waiver(_Model):
    id: str = Field(min_length=1, max_length=100)
    approver: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=4000)
    scope: WaiverScope
    expires_at: AwareDatetime
    remediation_owner: str | None = None


class WaiverPolicy(_Model):
    kind: Literal["susanoo.acceptance_waivers"]
    schema_version: Literal["1.0"] = "1.0"
    waivers: list[Waiver] = Field(default_factory=list, max_length=1000)


def load_waiver_policy(path: Path | None) -> WaiverPolicy | None:
    if path is None:
        return None
    try:
        return WaiverPolicy.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"Waiver policy not found: {path}") from None
    except ValidationError as error:
        raise ConfigError(f"Waiver policy {path} is invalid ({WAIVER_POLICY_KIND} expected): {_first(error)}") from None


class Acceptance(_Model):
    machine_gate: str
    machine_exit_code: int
    decision: Literal["accepted", "accepted_with_waivers", "not_accepted"]
    exit_code: int
    waivers_applied: bool
    applied: list[dict[str, str]] = Field(default_factory=list)
    expired: list[str] = Field(default_factory=list)
    unwaived: list[str] = Field(default_factory=list)


def evaluate_acceptance(report: RunReport, policy: WaiverPolicy | None, now: datetime | None = None) -> Acceptance:
    """Acceptance is a separate decision layered on the untouched machine result [Spec §13, §16; T16]."""
    now = now or datetime.now(UTC)
    machine = machine_exit_code(report)
    base = {"machine_gate": report.gate, "machine_exit_code": machine}
    if report.gate == "READY_FOR_ACCEPTANCE":
        return Acceptance(**base, decision="accepted", exit_code=EXIT_READY, waivers_applied=policy is not None)
    if policy is None:
        return Acceptance(**base, decision="not_accepted", exit_code=machine, waivers_applied=False)
    active = [w for w in policy.waivers if w.expires_at > now]
    expired = sorted(w.id for w in policy.waivers if w.expires_at <= now)
    applied: list[dict[str, str]] = []
    unwaived: list[str] = []
    for attempt in report.attempts:
        operational = []
        if attempt.cleanup_state == "failed":
            operational.append("cleanup_failed")
        if attempt.reconciliation_state in {"pending", "unresolved"}:
            operational.append("unresolved_side_effect")
        if attempt.required and attempt.execution_state != "completed":
            operational.append(f"required_{attempt.execution_state}")
        unwaived += [f"{attempt.case_id}:{item}" for item in operational]
        if not attempt.required or attempt.business_verdict == "PASS":
            continue
        open_checks = {o.check_id for o in attempt.outcomes if o.required and o.verdict != "PASS"}
        match = next(
            (
                w
                for w in active
                if w.scope.suite_id == report.suite_id
                and w.scope.case_id == attempt.case_id
                and (w.scope.agent is None or w.scope.agent == report.agent)
                and (not w.scope.check_ids or (open_checks and open_checks <= set(w.scope.check_ids)))
            ),
            None,
        )
        if match is None or operational:
            unwaived.append(f"{attempt.case_id}:required_{attempt.business_verdict.lower()}")
        else:
            applied.append({"waiver": match.id, "case_id": attempt.case_id, "verdict": attempt.business_verdict})
    if unwaived:
        return Acceptance(
            **base,
            decision="not_accepted",
            exit_code=machine,
            waivers_applied=True,
            applied=applied,
            expired=expired,
            unwaived=sorted(set(unwaived)),
        )
    return Acceptance(
        **base,
        decision="accepted_with_waivers",
        exit_code=EXIT_READY,
        waivers_applied=True,
        applied=applied,
        expired=expired,
    )


# --- executing a local run ----------------------------------------------------------------------


def prepare_run(
    run_dir: RunDir,
    suite: LocalSuite,
    *,
    agent_label: str,
    agent_spec: str | None,
    agent_cmd: list[str] | None,
    profile: LocalProfile,
    case_ids: list[str] | None,
    reproduces: dict[str, str] | None = None,
) -> RunManifest:
    """Freeze the suite and manifest before execution so every later view reads the same inputs."""
    from susanoo_evaluator import evaluator_digest

    selected = select_cases(suite, profile, case_ids)
    write_json(run_dir.suite_path, suite.model_dump(mode="json"))
    manifest = RunManifest(
        handle=run_dir.handle,
        suite_id=suite.id,
        suite_hash=suite.content_hash,
        synthetic=suite.synthetic,
        agent_label=agent_label,
        agent_spec=None if agent_cmd else agent_spec,
        agent_cmd=redact_argv(agent_cmd),
        agent_adapter="jsonl" if agent_cmd or agent_label.startswith("jsonl:") else agent_label.partition(":")[0],
        evaluator_digest=evaluator_digest(),
        created_at=datetime.now(UTC),
        case_filter=case_ids,
        profile=profile.model_dump(mode="json"),
        cases=[case_manifest(case) for case in selected],
        reproduces=reproduces,
    )
    run_dir.write_manifest(manifest)
    return manifest


def execute_run(
    run_dir: RunDir,
    suite: LocalSuite,
    *,
    agent_factory: Any,
    profile: LocalProfile,
    observer_outage: bool = False,
    cleanup_failure: bool = False,
) -> RunReport:
    """Run the frozen selection against a fresh local simulator; every case gets a new fixture namespace."""
    from .journal import Journal
    from .local import SimulatorServer, run_suite_sync

    manifest = run_dir.manifest()
    run_dir.write_status(RunStatus(state="running", pid=os.getpid()))
    journal = Journal(run_dir.journal_path)
    try:
        with SimulatorServer() as simulator:
            report = run_suite_sync(
                suite,
                agent_factory=agent_factory,
                agent_label=manifest.agent_label,
                journal=journal,
                simulator_url=simulator.base_url,
                simulator_admin_key=simulator.admin_key,
                profile=profile,
                observer_outage=observer_outage,
                cleanup_failure=cleanup_failure,
                case_ids=[case.case_id for case in manifest.cases],
            )
    except KeyboardInterrupt:
        run_dir.write_status(RunStatus(state="failed", exit_code=EXIT_OPERATIONAL, message="cancelled"))
        raise
    except Exception as error:
        message = f"Run did not finish: {type(error).__name__}: {error}"
        run_dir.write_status(RunStatus(state="failed", exit_code=EXIT_OPERATIONAL, message=message))
        raise
    finally:
        journal.close()
    write_json(run_dir.report_path, report.model_dump(mode="json"))
    run_dir.write_manifest(manifest.model_copy(update={"run_id": report.run_id}))
    run_dir.write_status(RunStatus(state="finished", exit_code=machine_exit_code(report), message=report.gate))
    return report


ReproductionOutcome = Literal["reproduced", "not_reproduced", "inconclusive"]


def reproduction_outcome(original: Any, fresh: Any) -> ReproductionOutcome:
    """Compare a fresh-fixture rerun of one case with the original attempt [Spec §16]."""
    if fresh.execution_state != "completed" or fresh.business_verdict == "UNKNOWN":
        return "inconclusive"
    if original.business_verdict == "UNKNOWN" or original.execution_state != "completed":
        return "inconclusive"
    if fresh.business_verdict != original.business_verdict:
        return "not_reproduced"
    if original.business_verdict == "FAIL":
        before = {o.check_id for o in original.outcomes if o.required and o.verdict == "FAIL"}
        after = {o.check_id for o in fresh.outcomes if o.required and o.verdict == "FAIL"}
        if before and after and not before & after:
            return "not_reproduced"
    return "reproduced"
