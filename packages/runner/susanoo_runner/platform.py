"""Customer-side platform transport. Dispatch is never resumed after authority is lost.

Only locally installed adapters execute here. The control plane supplies immutable inputs,
not executable code or an arbitrary command. Lost acknowledgements retain the local journal.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

import httpx
from pydantic import Field
from susanoo_contracts import CheckSpec, RunnerClaimResponse, claim_component_hashes, content_hash
from susanoo_sdk import ObservationQuery

from .executor import AttemptExecutor, CancelToken
from .journal import Journal, JournalError
from .local_security import secure_local_path
from .suite import LocalCase, LocalProfile, Limits


class PlatformProfile(LocalProfile):
    observation_kinds: dict[str, str] = Field(default_factory=dict)
    allowed_connection_adapters: list[str] = Field(min_length=1)
    cleanup_reserve: int = Field(default=3, ge=1, le=1000)
    allowed_connection_hashes: list[str] = Field(min_length=1)
    advanced_inputs: dict[str, dict] = Field(default_factory=dict)
    connection_bindings: dict[str, dict] = Field(default_factory=dict)
    max_reference_calls: int = Field(default=160, ge=0)
    max_output_bytes: int = Field(default=262144, ge=1, le=1048576)
    max_storage_bytes: int = Field(default=10485760, ge=1)


class PlatformClient:
    def __init__(self, url: str, token: str, *, transport=None):
        parsed = urlsplit(url)
        if (
            (
                parsed.scheme != "https"
                and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"})
            )
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Use HTTPS, or explicit loopback HTTP, without embedded credentials")
        self.http = httpx.Client(
            base_url=url.rstrip("/") + "/",
            timeout=2,
            follow_redirects=False,
            transport=transport,
            headers={"Authorization": f"Bearer {token}"},
        )

    def close(self):
        self.http.close()

    def post(self, path: str, body: dict):
        response = self.http.post(path.lstrip("/"), json=body)
        if response.status_code == 204:
            return None
        if not response.is_success:
            error = response.json().get("error", {})
            raise JournalError(error.get("code", "platform_error"), error.get("message", "Platform refused request"))
        return response.json()

    def claim(self, supported_job_kinds=None):
        value = self.post("runner/claims", {
            "supported_job_kinds": supported_job_kinds or ["outcome_suite", "preflight"]
        })
        if value is None:
            return None
        claim = RunnerClaimResponse.model_validate(value)
        verify_claim(claim)
        return claim


def verify_claim(claim: RunnerClaimResponse):
    expected = claim_component_hashes(claim.execution_manifest, claim.evaluator_context)
    if expected != claim.component_hashes or content_hash(expected) != claim.manifest_hash:
        raise JournalError("manifest_mismatch", "Claim contents do not match the component hashes")
    context = claim.agent_context
    if (context.attempt_id, context.run_id, context.namespace, context.deadline) != (
        claim.attempt_id,
        claim.run_id,
        claim.namespace,
        claim.deadline,
    ):
        raise JournalError("manifest_mismatch", "Agent-visible assignment identity differs from the claim")
    if claim.evaluator_context:
        pinned = claim.evaluator_context.case
        connection = claim.execution_manifest.connection
        if connection is None or content_hash(connection.data) != content_hash(context.connection):
            raise JournalError("manifest_mismatch", "Agent connection differs from the hashed execution connection")
        script = [step.model_dump(mode="json") for step in context.script]
        pinned_script = [step.model_dump(mode="json") for step in pinned.data.script]
        if context.case_id != pinned.id or content_hash(script) != content_hash(pinned_script):
            raise JournalError("manifest_mismatch", "Agent script differs from the hashed case")


def local_case(claim: RunnerClaimResponse, profile: PlatformProfile) -> LocalCase:
    pinned = claim.evaluator_context
    if pinned is None:
        versions = claim.execution_manifest.versions
        suite = next((v for v in versions if v.kind == "suite" and v.approved), None)
        if suite is None:
            raise JournalError("approved_suite_required", "Preflight needs a pinned approved suite and environment")
        by_id = {str(v.id): v for v in versions}
        case = by_id[str(suite.data.case_version_ids[0])]
        fixture = by_id[str(case.data.fixture_version_id)]
        checks = [by_id[str(v)] for v in case.data.check_version_ids]
    else:
        case, fixture, checks = pinned.case, pinned.fixture, pinned.checks
    connection = claim.agent_context.connection
    if connection is None:
        connection = next((v.data for v in claim.execution_manifest.versions if v.kind == "connection"), None)
    if connection is None or connection.adapter not in profile.allowed_connection_adapters:
        raise JournalError("unapproved_adapter", "Connection adapter is not locally approved")
    if content_hash(connection) not in profile.allowed_connection_hashes:
        raise JournalError("unapproved_connection", "Connection content hash is not locally approved")
    definitions, queries = [], {}
    for version in checks:
        check = CheckSpec.model_validate(version.data)
        definitions.append(check.evaluator_definition(version.id))
        kind = check.entity or profile.observation_kinds.get(check.observation)
        if not kind:
            raise JournalError("observation_mapping_required", "Observation entity must be explicitly configured")
        queries[check.observation] = ObservationQuery(
            name=check.observation,
            kind=kind,
            window_seconds=check.window_seconds,
            baseline=check.operator in {"transition", "unchanged", "forbidden_change"},
        )
    result = LocalCase(
        id=str(case.id),
        title=case.title,
        requirement_ids=[str(v.data.requirement_version_id) for v in checks],
        required=case.data.required,
        visibility=case.data.visibility,
        modality=case.data.modality,
        script=[step.model_dump(mode="json") for step in case.data.script],
        fixture=fixture.data,
        observations=list(queries.values()),
        checks=definitions,
    )
    reasons = profile.admit(result)
    if profile.data_mode == "synthetic" and not result.fixture.synthetic:
        reasons.append("Synthetic-only profile refuses non-synthetic fixture data")
    if reasons:
        raise JournalError("profile_refused", "; ".join(reasons))
    return result


class PlatformJournal(Journal):
    def __init__(self, path: Path, client: PlatformClient, claim: RunnerClaimResponse, cancel: CancelToken):
        super().__init__(path)
        for item in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
            if item.exists():
                secure_local_path(item)
        self.client, self.claim, self.cancel = client, claim, cancel
        self.lost_authority = False
        self.confirmed_until = 0.0

    @property
    def endpoint(self):
        return f"runner/attempts/{self.claim.attempt_id}"

    def control(self, *, renew=False):
        if self.lost_authority:
            raise JournalError("LEASE_EXPIRED", "Dispatch authority was lost; this attempt cannot resume")
        started = time.monotonic()
        try:
            result = self.client.post(
                self.endpoint + ("/heartbeat" if renew else "/control"), {"lease_token": self.claim.lease_token}
            )
            server = datetime.fromisoformat(result["server_time"].replace("Z", "+00:00"))
            expiry = datetime.fromisoformat(result["lease_expires_at"].replace("Z", "+00:00"))
            deadline = datetime.fromisoformat(result["deadline"].replace("Z", "+00:00"))
            self.confirmed_until = started + max(0, (min(expiry, deadline) - server).total_seconds())
            if time.monotonic() >= self.confirmed_until:
                raise JournalError("LEASE_EXPIRED", "Lease expired during the control request")
            if result["cancel_requested"]:
                self.cancel.cancel()
                if not any(e["type"] == "cancel_acknowledged" for e in self.events(str(self.claim.attempt_id))):
                    self.append_event(str(self.claim.attempt_id), "cancel_acknowledged", {})
                    self.flush()
            return result
        except Exception:
            self.lost_authority = True
            self.cancel.cancel()
            raise

    def mark_dispatching(self, attempt_id, step_id, *, scope=None):
        step = self.step(attempt_id, step_id)
        self.control()
        if self.cancel.requested and step.action_kind not in {"observe", "cleanup"}:
            raise JournalError("cancelled", "No new business action after cancellation")
        self.flush()
        kind = (
            "cleanup"
            if step.action_kind in {"fixture_prepare", "cleanup", "observe"}
            else ("reference" if step.action_kind == "reference_call" else "agent")
        )
        try:
            self.client.post(
                self.endpoint + "/reservations",
                {
                    "lease_token": self.claim.lease_token,
                    "operation_id": step.operation_id,
                    "budget_kind": kind,
                },
            )
        except Exception:
            self.lost_authority = True
            self.cancel.cancel()
            raise
        result = super().mark_dispatching(attempt_id, step_id, scope=scope)
        self.flush()
        # Both local intent and hosted dispatch acknowledgement precede the external call.
        if self.lost_authority or time.monotonic() >= self.confirmed_until:
            self.lost_authority = True
            raise JournalError("LEASE_EXPIRED", "No dispatch after an expired confirmation")
        return result

    def record_response(self, *args, **kwargs):
        # The response is durable even if its upload subsequently fails.
        result = super().record_response(*args, **kwargs)
        self.flush()
        return result

    def wire_events(self):
        result = []
        for event in self.events(str(self.claim.attempt_id)):
            payload = event["payload"]
            if event["type"] == "observation_recorded":
                payload = {
                    "observation_id": str(uuid5(NAMESPACE_URL, f"{self.claim.attempt_id}:{payload['observation']}"))
                }
            elif event["type"] == "check_recorded":
                payload = {"check_version_id": payload["check"]}
            elif event["type"] == "reconciliation_recorded":
                payload = {
                    "state": payload["state"],
                    "evidence_id": str(
                        uuid5(NAMESPACE_URL, f"{self.claim.attempt_id}:reconciliation:{event['step_id']}")
                    ),
                }
            result.append(
                {
                    name: event[name]
                    for name in (
                        "event_id",
                        "sequence",
                        "type",
                        "step_id",
                        "occurred_at",
                    )
                }
                | {"payload": payload}
            )
        return result

    def flush(self):
        if self.lost_authority:
            raise JournalError("LEASE_EXPIRED", "Evidence must be reconciled, not uploaded as live dispatch")
        events = self.wire_events()
        try:
            for start in range(0, len(events), 100):
                self.client.post(
                    self.endpoint + "/events",
                    {
                        "lease_token": self.claim.lease_token,
                        "events": events[start : start + 100],
                    },
                )
        except Exception:
            self.lost_authority = True
            self.cancel.cancel()
            raise

    def evidence_packet(self, result, *, preflight=False):
        observations, checks = [], []
        for item in self.evidence(result.attempt_id, "observation"):
            recorded = next(
                e["occurred_at"]
                for e in self.events(result.attempt_id)
                if e["type"] == "observation_recorded" and e["payload"]["observation"] == item["name"]
            )
            observations.append(
                {
                    "observation_id": str(uuid5(NAMESPACE_URL, f"{result.attempt_id}:{item['name']}")),
                    "name": item["name"],
                    "source": item["source"],
                    "captured_at": recorded,
                    "availability": item["availability"],
                    "completeness": item["completeness"],
                    "namespace": result.namespace,
                    "window_seconds": item["window_seconds"],
                    "window_elapsed": item["window_elapsed"],
                    "storage_mode": "local_only",
                    "values": {},
                    "redaction_version": "local-digest-v1",
                    "digest": content_hash(item),
                }
            )
        if not preflight:
            for outcome in result.outcomes:
                checks.append(
                    {
                        "check_version_id": outcome.check_id,
                        "evaluator_digest": result.evaluator_digest,
                        "verdict": outcome.verdict,
                        "reason": outcome.reason,
                        "reason_code": outcome.reason_code,
                        "observation_ids": [
                            str(uuid5(NAMESPACE_URL, f"{result.attempt_id}:{name}"))
                            for name in outcome.observation_names
                        ],
                        "measured": {},
                    }
                )
        return {"lease_token": self.claim.lease_token, "observations": observations, "check_results": checks}


async def execute_claim(
    client: PlatformClient,
    claim: RunnerClaimResponse,
    *,
    profile: PlatformProfile,
    journal_path: Path,
    agent_factory: Callable,
    fixtures,
):
    """Execute a single new claim; existing journal attempts are reconciliation-only."""
    verify_claim(claim)
    case = local_case(claim, profile)
    requested = claim.execution_manifest.limits
    limits = profile.narrow(Limits(**{name: getattr(requested, name) for name in Limits.model_fields}))
    if limits.model_dump() != {name: getattr(requested, name) for name in Limits.model_fields}:
        raise JournalError("profile_refused", "Claim limits exceed the locally approved profile")
    if requested.cleanup_reserve > profile.cleanup_reserve:
        raise JournalError("profile_refused", "Cleanup reserve exceeds the locally approved maximum")
    if profile.data_mode == "local_only" and claim.execution_manifest.policy.mode != "local_only":
        raise JournalError("profile_refused", "Local-only profile refuses hosted execution policy")
    cancel = CancelToken()
    journal = PlatformJournal(journal_path, client, claim, cancel)
    stop = asyncio.Event()

    async def monitor():
        tick = 0
        while not stop.is_set():
            try:
                await asyncio.to_thread(journal.control, renew=tick % 5 == 0)
            except Exception:
                return
            tick += 1
            try:
                await asyncio.wait_for(stop.wait(), 2)
            except TimeoutError:
                pass

    monitoring = None
    try:
        journal.control(renew=True)
        monitoring = asyncio.create_task(monitor())
        executor = AttemptExecutor(
            journal, run_id=str(claim.run_id), limits=limits, agent_factory=agent_factory, fixtures=fixtures
        )
        result = await executor.run(case, cancel, assignment=claim)
        packet = journal.evidence_packet(result, preflight=claim.kind == "preflight")
        if journal.lost_authority:
            # This endpoint never renews authority. A still-active server attempt may need its sweeper first.
            return result.model_copy(
                update={
                    "execution_state": "interrupted",
                    "reconciliation_state": "pending",
                    "reason": result.reason + "; Platform authority lost; local evidence is not a hosted completion",
                }
            )
        journal.flush()
        client.post(journal.endpoint + "/evidence", packet)
        verdict = result.business_verdict
        if claim.kind == "preflight":
            observed = journal.evidence(result.attempt_id, "observation")
            health = {
                "job_kind": "preflight",
                "invocation_ok": bool(result.turns)
                and all(turn.dispatch_state == "acknowledged" for turn in result.turns),
                "observer_ok": bool(observed) and all(o["availability"] == "available" for o in observed),
                "cleanup_ok": result.cleanup_state == "succeeded",
            }
            journal.append_event(result.attempt_id, "job_result", health)
            journal.flush()
            verdict = (
                "PASS" if all(health[name] for name in ("invocation_ok", "observer_ok", "cleanup_ok")) else "UNKNOWN"
            )
        client.post(
            journal.endpoint + "/finalize",
            {
                "lease_token": claim.lease_token,
                "execution_state": result.execution_state,
                "business_verdict": verdict,
                "cleanup_state": result.cleanup_state,
                "reconciliation_state": result.reconciliation_state,
                "reason": result.reason,
                "final_sequence": len(journal.events(result.attempt_id)),
            },
        )
        return result
    finally:
        stop.set()
        if monitoring:
            with contextlib.suppress(Exception):
                await monitoring
        journal.close()


def reconcile_stored(client: PlatformClient, journal_path: Path, attempt_id: str):
    """Upload an interrupted journal as late evidence. Never execute, clean up or redispatch."""
    source = Journal(journal_path)
    try:
        for item in (journal_path, Path(str(journal_path) + "-wal"), Path(str(journal_path) + "-shm")):
            if item.exists():
                secure_local_path(item)
        source.recover()
        stored = source.attempt(attempt_id)
        import json

        claim = RunnerClaimResponse.model_validate(json.loads(stored["manifest"]))
    finally:
        source.close()
    if str(claim.attempt_id) != attempt_id:
        raise JournalError("manifest_mismatch", "Stored assignment identity differs from the requested attempt")
    cancel = CancelToken()
    journal = PlatformJournal(journal_path, client, claim, cancel)
    try:
        events = journal.wire_events()
        packet = journal.evidence_packet(SimpleNamespace(attempt_id=attempt_id, namespace=claim.namespace, outcomes=[]))
        result = None
        for start in range(0, max(1, len(events)), 100):
            result = client.post(
                journal.endpoint + "/reconciliations",
                {
                    "lease_token": claim.lease_token,
                    "state": "unresolved",
                    "rationale": "Interrupted journal; no dispatch authority. Operation read-back required.",
                    "observations": packet["observations"],
                    "late_events": events[start : start + 100],
                    "cleanup_state": stored["cleanup_state"] if stored["cleanup_state"] != "pending" else None,
                },
            )
        return result
    finally:
        journal.close()
