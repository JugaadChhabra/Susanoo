"""Locally approved advanced jobs. No command, endpoint or file path comes from a claim."""

import asyncio
import contextlib
from types import SimpleNamespace
from uuid import uuid4

import httpx
from pydantic import BaseModel
from susanoo_contracts import content_hash
from susanoo_sdk import SnapshotScope
from susanoo_simulator import FixtureSeed, FaultProfile
from susanoo_simulator.environment import SimulatorEnvironment

from .fidelity import ActionPlan, EnvironmentRef, FidelityContract, compare
from .imports import Importer, ImportLimits
from .incidents import group_incidents, incidents_from_records, propose_draft
from .journal import JournalError
from .platform import PlatformJournal, verify_claim
from .executor import CancelToken

SUPPORTED = {
    "simulator_inspect",
    "simulator_reset",
    "action_comparison",
    "reduce_reproduction",
    "trace_import",
    "incident_draft",
    "fresh_agent_pair",
    "source_import",
    "build",
    "repair_retest",
    "calibration",
    "collision",
    "probe",
    "voice",
}


def approval_key(claim):
    # Pin policy, budgets, artifact digests and the exact repair proposal as well as revisions.
    return content_hash({"kind": claim.kind, "execution_manifest": claim.execution_manifest.model_dump(mode="json")})


def owned_storage_bytes(journal):
    total, entries = 0, 0
    for root in journal.path.parent.glob(journal.path.name + "*"):
        for path in [root] if not root.is_dir() else root.rglob("*"):
            entries += 1
            if entries > 50000 or path.is_symlink():
                raise JournalError("storage_limit", "Owned runner storage is not safely bounded")
            if path.is_file():
                total += path.stat().st_size
    return total


class GuardedTransport(httpx.AsyncBaseTransport):
    """Durable intent, local+hosted reservation and fresh authority before EVERY HTTP request."""

    def __init__(self, journal, limit):
        self.journal, self.limit = journal, limit
        self.transport = httpx.AsyncHTTPTransport(retries=0, trust_env=False)
        self.count = 0

    async def handle_async_request(self, request):
        self.count += 1
        journal = self.journal
        if len(request.content) > self.limit:
            raise JournalError("input_limit", "Environment request exceeded the approved byte limit")
        request.headers["Accept-Encoding"] = "identity"
        attempt = str(journal.claim.attempt_id)
        if owned_storage_bytes(journal) >= journal.claim.execution_manifest.limits.max_storage_bytes:
            raise JournalError("storage_limit", "Local journal exceeded the approved storage budget")
        operation = "/operations/" in request.url.path
        kind = (
            "reference_call"
            if operation
            else "cleanup"
            if request.method == "DELETE"
            else ("fixture_prepare" if request.method == "POST" and request.url.path == "/fixtures" else "observe")
        )
        budget = "reference" if operation else "cleanup"
        scope = f"{attempt}:{budget}"
        step = journal.prepare_step(
            attempt,
            f"http-{self.count}",
            kind,
            content_hash(
                {
                    "method": request.method,
                    "path": request.url.path,
                    "body": request.content.hex(),
                }
            ),
        )
        if not journal.reserve(scope, budget, step.operation_id):
            journal.abandon_step(attempt, step.step_id, "budget_exceeded")
            raise JournalError("budget_exceeded", "Advanced job request budget exhausted")
        journal.mark_dispatching(attempt, step.step_id, scope=scope)
        try:
            response = await self.transport.handle_async_request(request)
            chunks, size = [], 0
            try:
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise JournalError("encoding_denied", "Compressed environment responses are not approved")
                async for chunk in response.aiter_raw():
                    size += len(chunk)
                    if size > self.limit:
                        raise JournalError("output_limit", "Environment response exceeded the approved byte limit")
                    chunks.append(chunk)
            finally:
                await response.aclose()
            if response.status_code >= 500 or (kind == "cleanup" and response.status_code not in {204, 404}):
                raise JournalError("environment_uncertain", "Environment request requires reconciliation")
            journal.record_response(
                attempt,
                step.step_id,
                dispatch_state="acknowledged",
                side_effect_state=("none_expected" if request.method in {"GET", "HEAD"} else "requires_observation"),
                response={"http_status": response.status_code},
            )
            return httpx.Response(response.status_code, headers=response.headers, content=b"".join(chunks))
        except BaseException:
            if journal.step(attempt, step.step_id).dispatch_state == "dispatching":
                journal.record_response(
                    attempt,
                    step.step_id,
                    dispatch_state="uncertain",
                    side_effect_state="requires_observation",
                    error="environment_request_incomplete",
                )
            raise

    async def aclose(self):
        await self.transport.aclose()


def _version(claim, identifier=None, kind=None):
    return next(
        v
        for v in claim.execution_manifest.versions
        if (identifier is None or v.id == identifier) and (kind is None or v.kind == kind)
    )


def _contract(claim, inputs):
    version = _version(claim, kind="fidelity")
    spec = version.data
    environments = [
        _version(claim, identifier=value).data for value in (spec.simulator_version_id, spec.reference_version_id)
    ]
    fixtures = [_version(claim, identifier=value.fixture_version_id).data for value in environments]
    seeds = [FixtureSeed.model_validate(fixture.recipe["seed"]) for fixture in fixtures]

    def initial(seed):
        value = seed.model_dump(mode="json")
        value["entities"].pop("policy", None)
        return value

    if content_hash(initial(seeds[0])) != content_hash(initial(seeds[1])):
        raise JournalError("fixture_mismatch", "Comparison requires the same pinned initial fixture")
    seed = seeds[0]
    if any(not set(spec.operations) <= set(env.allowed_operations) for env in environments):
        raise JournalError("operation_denied", "Fidelity operations exceed a pinned environment's permissions")
    refs = [
        EnvironmentRef(
            label=label,
            domain_version=env.version,
            policy=(value.entities.get("policy") or [{}])[0],
            faults=FaultProfile.model_validate(env.fault_profile),
        )
        for label, env, value in zip(("simulator", "reference"), environments, seeds, strict=True)
    ]
    return FidelityContract(
        id=str(version.id),
        synthetic=fixtures[0].synthetic,
        simulator=refs[0],
        reference=refs[1],
        fixture=seed,
        operations=spec.operations,
        entity_kinds=inputs["entity_kinds"],
        normalizers=spec.normalizers,
        intentional_simplifications=[value.model_dump(mode="json") for value in spec.intentional_simplifications],
        reference_limitations=[
            *spec.reference_limitations,
            *["Declared omission: " + value for value in spec.intentional_omissions],
        ],
        consistency_window_seconds=spec.consistency_window_seconds,
    )


class GuardedAgent:
    def __init__(self, adapter, journal):
        self.adapter, self.journal = adapter, journal
        self.counter = 0
        self.identity = uuid4().hex

    async def _call(self, name, *args):
        journal, attempt = self.journal, str(self.journal.claim.attempt_id)
        self.counter += 1
        kind = "cleanup" if name in {"close_session", "cancel"} else "agent_invoke"
        budget = "cleanup" if kind == "cleanup" else "agent"
        scope = f"{attempt}:{budget}"
        step = journal.prepare_step(
            attempt,
            f"agent-{self.identity}-{self.counter}",
            kind,
            content_hash({"method": name, "ordinal": self.counter}),
        )
        if not journal.reserve(scope, budget, step.operation_id):
            journal.abandon_step(attempt, step.step_id, "budget_exceeded")
            raise JournalError("budget_exceeded", "Parent agent allowance exhausted")
        journal.mark_dispatching(attempt, step.step_id, scope=scope)
        try:
            result = await getattr(self.adapter, name)(*args)
            from susanoo_contracts import canonical_json

            if result is not None:
                value = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
                if len(canonical_json(value).encode()) > journal.claim.execution_manifest.limits.max_output_bytes:
                    raise JournalError("output_limit", "Agent response exceeded the pinned byte limit")
        except BaseException:
            journal.record_response(
                attempt,
                step.step_id,
                dispatch_state="uncertain",
                side_effect_state="requires_observation",
                error="agent_request_incomplete",
            )
            raise
        journal.record_response(
            attempt, step.step_id, dispatch_state="acknowledged", side_effect_state="requires_observation"
        )
        return result

    async def start_session(self, context):
        return await self._call("start_session", context)

    async def invoke(self, session, message, context):
        return await self._call("invoke", session, message, context)

    async def close_session(self, session):
        return await self._call("close_session", session)

    async def cancel(self, invocation_id):
        return await self._call("cancel", invocation_id)

    async def aclose(self):
        close = getattr(self.adapter, "aclose", None)
        if close is not None:
            await close()


async def _produce(claim, inputs, environment, journal, profile, agent_factory, agent_factories=None):
    if claim.kind in {"source_import", "build", "repair_retest"}:
        from .platform_source_jobs import produce

        return await produce(
            claim.kind,
            claim,
            inputs,
            journal=journal,
            profile=profile,
            agent_factory=agent_factory,
            environment=environment,
        )
    if claim.kind in {"calibration", "collision", "probe", "voice"}:
        from .platform_science_jobs import produce

        return await produce(
            claim, inputs, journal=journal, environment=environment, profile=profile, agent_factory=agent_factory
        )
    if claim.kind == "fresh_agent_pair":
        from .platform import local_case
        from .impact import ImpactRunner
        from .journal import Journal
        from .suite import Limits

        case = local_case(claim, profile)
        contract = _contract(claim, inputs)
        child = Journal(str(journal.path) + ".impact")
        limits = Limits(**{key: getattr(claim.execution_manifest.limits, key) for key in Limits.model_fields})

        async def guarded_runner(factory):
            runner = ImpactRunner(
                child,
                simulator_url="http://unused.invalid",
                admin_key="unused",
                limits=limits,
                agent_factory=lambda: GuardedAgent(factory(), journal),
            )
            await runner.fixtures._client.aclose()
            runner.fixtures.base_url = str(environment._client.base_url).rstrip("/")
            runner.fixtures._admin_key = environment._admin_key
            runner.fixtures._client = environment._client
            return runner

        try:
            if inputs.get("mode") == "two_by_two":
                from .impact import TwoByTwoCell

                if not agent_factories or set(agent_factories) != {"old", "new"}:
                    raise JournalError("profile_refused", "Two-by-two requires two explicit local agent bindings")
                repetitions = inputs.get("repetitions", 1)
                if isinstance(repetitions, bool) or not isinstance(repetitions, int) or not 1 <= repetitions <= 10:
                    raise JournalError("input_limit", "Repetitions must be an integer from one to ten")
                cells = []
                for agent_label in ("old", "new"):
                    runner = await guarded_runner(agent_factories[agent_label])
                    for environment_label, env in (("old", contract.simulator), ("new", contract.reference)):
                        results = [
                            await runner.run_side(case, env, f"2x2-{agent_label}-{environment_label}")
                            for _ in range(repetitions)
                        ]
                        cells.append(TwoByTwoCell(agent=agent_label, environment=environment_label, results=results))

                class MatrixReport(BaseModel):
                    cells: list[TwoByTwoCell]

                return [("two_by_two", MatrixReport(cells=cells))], "UNKNOWN"
            if agent_factory is None:
                raise JournalError("profile_refused", "Fresh impact requires a locally approved agent")
            runner = await guarded_runner(agent_factory)
            pair = await runner.pair(case, contract.simulator, contract.reference)
            return [("task_impact", pair)], "UNKNOWN"
        finally:
            child.close()
    if claim.kind in {"simulator_inspect", "simulator_reset"}:
        env = _version(claim, kind="environment").data
        fixture = _version(claim, identifier=env.fixture_version_id).data
        seed = FixtureSeed.model_validate(fixture.recipe["seed"])
        await environment.prepare(claim.namespace, seed, FaultProfile.model_validate(env.fault_profile))
        try:
            scope = SnapshotScope(namespace=claim.namespace, kinds=inputs["entity_kinds"])
            snapshot = await environment.snapshot(scope)
            if claim.kind == "simulator_reset":
                await environment.destroy(claim.namespace)
                await environment.prepare(claim.namespace, seed, FaultProfile.model_validate(env.fault_profile))
                reset = await environment.snapshot(scope)
                if reset.digest != snapshot.digest:
                    raise JournalError("reset_mismatch", "Reset did not restore the pinned initial state")
                snapshot = reset
            return [("simulator_state", snapshot)], "UNKNOWN"
        finally:
            await environment.destroy(claim.namespace)
    if claim.kind in {"action_comparison", "reduce_reproduction"}:
        contract = _contract(claim, inputs)
        plan = ActionPlan.model_validate(inputs["plan"])
        if not {a.operation for a in plan.actions} <= set(contract.operations):
            raise JournalError("operation_denied", "Action plan exceeds the pinned contract")
        if claim.kind == "action_comparison":
            result = await compare(contract, plan, environment, environment)
            uncertain = any(
                side.status in {"uncertain", "error"}
                for step in result.steps
                for side in (step.simulator, step.reference)
            )
            verdict = "UNKNOWN" if result.status != "compared" or uncertain else "PASS" if result.matched else "FAIL"
            return [("comparison", result)], verdict
        from .reduce import reduce_reproduction

        category = inputs["violation_category"]
        result = await reduce_reproduction(
            contract,
            plan,
            lambda result: any(c.category == category for c in result.candidates),
            environment,
            environment,
            budget=inputs.get("reduction_budget", 10),
        )
        return [("reduction", result)], "UNKNOWN"
    connection = _version(claim, kind="connection").data
    if connection.family != "I13":
        raise JournalError("evidence_only_required", "Imports and incident drafts require an evidence-only connection")
    raw = inputs["data"].encode("utf-8")
    if len(raw) > claim.execution_manifest.limits.max_output_bytes:
        raise JournalError("input_limit", "Import exceeds the approved byte budget")
    importer = Importer(str(claim.run_id), ImportLimits(max_batch_bytes=len(raw) or 1, max_records=1000))
    method = {"jsonl": importer.import_jsonl, "csv": importer.import_csv}[inputs.get("format", "jsonl")]
    batch = method(raw, "locally-approved-input")
    if claim.kind == "trace_import":
        return [("import", batch)], "UNKNOWN"
    drafts = [
        propose_draft(group, domain=inputs["domain"])
        for group in group_incidents(incidents_from_records(batch.records))
    ]
    if not drafts:
        raise JournalError("no_incidents", "No candidate incident was observed; no draft was fabricated")
    return [("incident", draft) for draft in drafts], "UNKNOWN"


async def execute_advanced(
    client, claim, *, profile, journal_path, simulator_url, simulator_key, agent_factory=None, agent_factories=None
):
    verify_claim(claim)
    if claim.kind not in SUPPORTED or approval_key(claim) not in profile.advanced_inputs:
        raise JournalError("profile_refused", "Advanced job inputs are not approved on this runner")
    limits = claim.execution_manifest.limits
    if limits.cleanup_reserve > profile.cleanup_reserve or limits.max_reference_calls > profile.max_reference_calls:
        raise JournalError("profile_refused", "Advanced limits exceed local authority")
    if limits.max_output_bytes > profile.max_output_bytes or limits.max_storage_bytes > profile.max_storage_bytes:
        raise JournalError("profile_refused", "Advanced byte budgets exceed local authority")
    if limits.case_timeout_seconds > profile.case_timeout_seconds:
        raise JournalError("profile_refused", "Advanced deadline exceeds local authority")
    if profile.data_mode != "synthetic" or claim.execution_manifest.policy.mode != "synthetic":
        raise JournalError("profile_refused", "Advanced hosted jobs currently require synthetic input")
    inputs = profile.advanced_inputs[approval_key(claim)]
    for version in claim.execution_manifest.versions:
        if version.kind == "environment":
            if version.data.adapter != "susanoo_simulator":
                raise JournalError("unapproved_adapter", "This worker cannot reinterpret another environment adapter")
            if profile.allowed_operations and not set(version.data.allowed_operations) <= set(
                profile.allowed_operations
            ):
                raise JournalError("profile_refused", "Environment operations exceed the locally approved profile")
    cancel = CancelToken()
    journal = PlatformJournal(journal_path, client, claim, cancel)
    attempt = str(claim.attempt_id)
    journal.begin_attempt(
        attempt_id=attempt,
        run_id=str(claim.run_id),
        case_id=claim.kind,
        namespace=claim.namespace,
        manifest=claim.model_dump(mode="json"),
        manifest_hash=claim.manifest_hash,
        fencing_generation=claim.fencing_generation,
        deadline=claim.deadline,
    )
    journal.set_budget(f"{attempt}:cleanup", "cleanup", limits.cleanup_reserve)
    journal.set_budget(f"{attempt}:reference", "reference", limits.max_reference_calls)
    journal.set_budget(f"{attempt}:agent", "agent", min(limits.max_agent_invocations, profile.max_agent_invocations))
    environment = SimulatorEnvironment(simulator_url, simulator_key, label="local approved simulator")
    await environment._client.aclose()
    environment._client = httpx.AsyncClient(
        base_url=simulator_url,
        follow_redirects=False,
        trust_env=False,
        transport=GuardedTransport(journal, limits.max_output_bytes),
        timeout=limits.invocation_timeout_seconds,
    )
    stop = asyncio.Event()

    async def monitor():
        while not stop.is_set():
            try:
                await asyncio.to_thread(journal.control, renew=True)
                await asyncio.wait_for(stop.wait(), 2)
            except TimeoutError:
                continue
            except Exception:
                return

    task = None
    state, verdict, reason = "completed", "UNKNOWN", ""
    cleanup, reconciliation = "succeeded", "not_needed"
    try:
        journal.control(renew=True)
        task = asyncio.create_task(monitor())
        produced = await asyncio.wait_for(
            _produce(claim, inputs, environment, journal, profile, agent_factory, agent_factories),
            limits.case_timeout_seconds,
        )
        reports, verdict = produced[:2]
        summary = produced[2] if len(produced) == 3 else None
        identifiers = []
        for index, (kind, payload) in enumerate(reports):
            body = {
                "lease_token": claim.lease_token,
                "report_type": kind,
                "report_key": f"result-{index}",
                "audience": "reviewers" if any(v.hidden for v in claim.execution_manifest.versions) else "project",
                "payload": payload.model_dump(mode="json"),
            }
            from susanoo_contracts import canonical_json

            used = owned_storage_bytes(journal)
            if used + len(canonical_json(body).encode()) > limits.max_storage_bytes:
                raise JournalError("storage_limit", "Report exceeds the remaining local storage budget")
            journal.store_evidence(attempt, "advanced_report", str(index), body)
            record = client.post(journal.endpoint + "/reports", body)
            journal.store_evidence(attempt, "advanced_receipt", str(index), record)
            identifiers.append(record["id"])
        if claim.kind == "calibration":
            if len(reports) != 1 or reports[0][0] not in {"calibration", "deterministic_calibration"}:
                raise JournalError("incomplete_evidence", "Calibration needs its actual measured control report")
            from susanoo_contracts import CalibrationResult

            payload = reports[0][1]
            deterministic = reports[0][0] == "deterministic_calibration"
            summary = CalibrationResult(
                control_set_hash=payload.control_set_hash,
                controls_verified=payload.accepted,
                missed_defects=len(payload.missed_defects) + len(payload.heldout_missed_defects)
                if deterministic
                else len(payload.false_passes),
                false_alarms=len(payload.false_alarms) + len(payload.heldout_false_alarms)
                if deterministic
                else len(payload.false_fails),
                report_ids=identifiers,
            )
            uncertain = payload.inconclusive if deterministic else payload.errors
            verdict = "UNKNOWN" if uncertain else "PASS" if payload.accepted else "FAIL"
        if summary is None:
            summary = {"job_kind": claim.kind, "status": "succeeded", "report_ids": identifiers}
        else:
            summary = summary.model_dump(mode="json")
            if claim.kind in {"source_import", "build"}:
                summary["report_ids"] = identifiers
            if claim.kind == "repair_retest":
                if len(identifiers) != 1:
                    raise JournalError("incomplete_evidence", "Repair requires an assigned typed validation report")
                summary["report_id"] = identifiers[0]
        journal.append_event(attempt, "job_result", summary)
        journal.append_event(attempt, "cleanup_recorded", {"state": "succeeded"})
        journal.flush()
    except Exception as error:
        state = "interrupted" if journal.lost_authority else "cancelled" if cancel.requested else "error"
        verdict = "UNKNOWN"
        reason = error.code if isinstance(error, JournalError) else type(error).__name__
        journal.mark_inflight_uncertain(attempt, reason)
        cleanup, reconciliation = "failed", "unresolved"
    finally:
        stop.set()
        if task:
            with contextlib.suppress(Exception):
                await task
        await environment._client.aclose()
    journal.finalize(
        attempt,
        execution_state=state,
        business_verdict=verdict,
        cleanup_state=cleanup,
        reconciliation_state=reconciliation,
        reason=reason,
    )
    try:
        if not journal.lost_authority:
            client.post(
                journal.endpoint + "/finalize",
                {
                    "lease_token": claim.lease_token,
                    "execution_state": state,
                    "business_verdict": verdict,
                    "cleanup_state": cleanup,
                    "reconciliation_state": reconciliation,
                    "reason": reason,
                    "final_sequence": len(journal.events(attempt)),
                },
            )
        return SimpleNamespace(
            attempt_id=attempt,
            execution_state=state,
            business_verdict=verdict,
            cleanup_state=cleanup,
            reconciliation_state=reconciliation,
            reason=reason,
        )
    finally:
        journal.close()
