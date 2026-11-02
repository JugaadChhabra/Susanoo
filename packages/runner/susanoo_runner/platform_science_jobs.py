"""Locally approved scientific jobs backed by the runner's existing algorithms.

The hosted claim selects immutable revisions only. Executable actor/probe inputs are
read from the locally approved runner profile and checked against that graph.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
import re
import tempfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import websockets

from susanoo_connectors.media.audio import AudioClip, MediaStore, StoragePolicy
from susanoo_connectors.media.metrics import (
    NormalizationPolicy,
    ReferenceTranscript,
    audio_metrics,
)
from susanoo_connectors.media.websocket_media import (
    MediaSocketConnection,
    ScriptedAudioTurn,
    WebSocketMediaAdapter,
)
from susanoo_contracts import canonical_json, content_hash
from susanoo_evaluator import evaluator_digest, parse_check
from susanoo_evaluator.calibration import Control, calibrate, control_set_hash
from susanoo_evaluator.semantic import (
    ExternalDataPermission,
    GraderConfig,
    GraderRequest,
    LabeledExample,
    SemanticThresholds,
    build_request,
    calibrate_grader,
)
from susanoo_runner.collision import (
    ActorScript,
    CollisionSchedule,
    ReadBeforeWrite,
    run_collision,
)
from susanoo_runner.fidelity import ActionPlan, ComparisonResult, compare
from susanoo_runner.journal import JournalError
from susanoo_runner.probing import (
    ProbeCandidate,
    ProbeOutcome,
    account,
    compare as compare_probes,
    plan_probes,
    run_probes,
)
from susanoo_simulator import FaultProfile, FixtureSeed
from susanoo_simulator.domains.shared_ledger import DOMAIN as COLLISION_DOMAIN


def _version(claim, *, kind: str, identifier: str | None = None):
    for version in claim.execution_manifest.versions:
        if version.kind == kind and (identifier is None or str(version.id) == str(identifier)):
            return version
    raise JournalError("pinned_input_missing", f"Claim has no pinned {kind} revision")


def _reject(message: str, code: str = "locally_approved_input_required"):
    raise JournalError(code, message)


def _fixture(claim, environment_version):
    fixture = _version(claim, kind="fixture", identifier=environment_version.data.fixture_version_id)
    if content_hash(fixture.data.recipe) != fixture.data.digest:
        _reject("Pinned fixture recipe digest is invalid", "fixture_mismatch")
    seed = FixtureSeed.model_validate(fixture.data.recipe["seed"])
    if not fixture.data.synthetic:
        _reject("Science jobs require a synthetic pinned fixture", "fixture_mismatch")
    return fixture, seed


def _record_local(journal, claim, name: str, payload: Any):
    """Journal a bounded local computation without claiming it was an external dispatch."""
    attempt = str(claim.attempt_id)
    body = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload
    encoded = canonical_json(body).encode("utf-8")
    current = sum(path.stat().st_size for path in journal.path.parent.glob(journal.path.name + "*") if path.is_file())
    media_root = journal.path.parent / "media" / attempt
    if media_root.exists():
        current += sum(path.stat().st_size for path in media_root.rglob("*") if path.is_file())
    if current + len(encoded) > claim.execution_manifest.limits.max_storage_bytes:
        _reject("Local science evidence exceeds the approved storage budget", "storage_limit")
    journal.store_evidence(attempt, "science_job", name, body)


async def _collision(claim, inputs, journal, profile):
    version = _version(claim, kind="collision")
    spec = version.data
    env_version = _version(claim, kind="environment", identifier=spec.environment_version_id)
    if env_version.data.adapter != "susanoo_simulator":
        _reject("Collision producer requires the locally installed simulator adapter", "unapproved_adapter")
    fixture, seed = _fixture(claim, env_version)
    if seed.domain != COLLISION_DOMAIN.name or seed.domain_version != COLLISION_DOMAIN.version:
        _reject("Collision tests require the dedicated shared-ledger fixture", "unsupported_collision_domain")
    if not isinstance(inputs, dict):
        _reject("Collision inputs must be a locally approved actor-script mapping")
    actor_names = [f"actor-{index + 1}" for index in range(spec.actors)]
    scripts = inputs.get("actor_scripts")
    if (
        not isinstance(scripts, dict)
        or set(scripts) != set(actor_names)
        or any(not isinstance(steps, list) for steps in scripts.values())
    ):
        _reject("Locally approved collision inputs must provide one fixed script per configured actor")
    barriers = []
    for operation_name in spec.barriers:
        operation = COLLISION_DOMAIN.operations.get(operation_name)
        if not operation or not operation.mutating:
            _reject(f"Pinned collision barrier {operation_name!r} is not a shared-ledger write", "invalid_schedule")
        if any(not any(item.get("operation") == operation_name for item in scripts[actor]) for actor in actor_names):
            _reject(f"Every actor must reach the pinned barrier operation {operation_name!r}", "invalid_schedule")
        barriers.append(ReadBeforeWrite(actors=actor_names))
    actor_functions: dict[str, ActorScript] = {}
    for actor in actor_names:
        raw_steps = scripts[actor]
        if not isinstance(raw_steps, list) or not raw_steps or len(raw_steps) > 100:
            _reject(f"Actor {actor} needs a bounded, non-empty operation script")
        checked = []
        for raw in raw_steps:
            if not isinstance(raw, dict):
                _reject("Actor script steps must be objects", "invalid_schedule")
            if set(raw) - {"operation", "request", "key"}:
                _reject("Actor script steps accept only operation, request and idempotency key", "invalid_schedule")
            operation_name = raw.get("operation")
            if operation_name not in env_version.data.allowed_operations:
                _reject(f"Operation {operation_name!r} is not approved by the pinned environment", "operation_denied")
            operation = COLLISION_DOMAIN.operations.get(operation_name)
            actor_spec = seed.actors.get(actor)
            if operation is None or actor_spec is None or operation_name not in actor_spec.operations:
                _reject(f"Actor {actor} is not authorized for {operation_name!r} by the fixture", "operation_denied")
            request = raw.get("request", {})
            if operation.request_model:
                request = operation.request_model.model_validate(request).model_dump(mode="json")
            if len(canonical_json(request).encode("utf-8")) > claim.execution_manifest.limits.max_output_bytes:
                _reject("Actor request exceeds the approved byte budget", "input_limit")
            checked.append((operation_name, request, raw.get("key")))

        async def run(proxy, sequence=tuple(checked), actor_name=actor):
            for operation, request, key in sequence:
                if journal.cancel.requested:
                    _reject("Collision actor stopped after cancellation", "cancelled")
                await asyncio.to_thread(journal.control)
                attempt = str(claim.attempt_id)
                current_storage = sum(
                    path.stat().st_size for path in journal.path.parent.glob(journal.path.name + "*") if path.is_file()
                )
                action_size = len(canonical_json({"operation": operation, "request": request}).encode("utf-8"))
                if current_storage + action_size > claim.execution_manifest.limits.max_storage_bytes:
                    _reject("Collision actor action exceeds the approved storage budget", "storage_limit")
                step_id = f"collision-{actor_name}-{uuid4().hex}"
                step = await asyncio.to_thread(
                    journal.prepare_step,
                    attempt,
                    step_id,
                    "agent_invoke",
                    content_hash({"actor": actor_name, "operation": operation, "request": request, "key": key}),
                )
                scope = f"{attempt}:agent"
                if not await asyncio.to_thread(journal.reserve, scope, "agent", step.operation_id):
                    await asyncio.to_thread(journal.abandon_step, attempt, step_id, "budget_exceeded")
                    _reject("Collision actors exhausted the approved invocation budget", "budget_exceeded")
                await asyncio.to_thread(journal.mark_dispatching, attempt, step_id, scope=scope)
                try:
                    result = await proxy.call(operation, request, key=key)
                except Exception as error:
                    if getattr(error, "code", None) == "permission_revoked":
                        await asyncio.to_thread(
                            journal.record_response,
                            attempt,
                            step_id,
                            dispatch_state="rejected_before_effect",
                            side_effect_state="none_expected",
                            error="permission_revoked",
                        )
                    else:
                        await asyncio.to_thread(
                            journal.record_response,
                            attempt,
                            step_id,
                            dispatch_state="uncertain",
                            side_effect_state="requires_observation",
                            error=type(error).__name__,
                        )
                    raise
                await asyncio.to_thread(
                    journal.record_response,
                    attempt,
                    step_id,
                    dispatch_state="acknowledged",
                    side_effect_state="requires_observation",
                    response={"status": result.status, "code": result.code},
                )

        actor_functions[actor] = run
    total_calls = sum(len(scripts[actor]) for actor in actor_names) * spec.repetitions
    invocation_cap = min(
        claim.execution_manifest.limits.max_agent_invocations,
        profile.max_agent_invocations,
        spec.limits.max_agent_invocations,
    )
    if total_calls > invocation_cap:
        _reject("Pinned collision schedule exceeds the authorized invocation budget", "budget_exceeded")
    input_size = len(canonical_json(scripts).encode("utf-8"))
    if input_size > claim.execution_manifest.limits.max_output_bytes:
        _reject("Collision actor scripts exceed the approved byte budget", "input_limit")
    schedule = CollisionSchedule(
        id=str(version.id),
        namespace=claim.namespace.replace(":", "-")[:80],
        intentionally_shared=True,
        actors=actor_names,
        barriers=barriers,
        repetitions=spec.repetitions,
        deadline_seconds=min(10.0, claim.execution_manifest.limits.case_timeout_seconds),
        max_calls_per_actor=max(len(scripts[a]) for a in actor_names),
    )
    # The existing collision engine is a local deterministic SQLite simulation. It does
    # not connect to a customer service; its disposable state is scoped to this temp dir.
    with tempfile.TemporaryDirectory(prefix="susanoo-collision-") as root:
        if journal.cancel.requested:
            _reject("Collision job was cancelled before the local simulation started", "cancelled")
        await asyncio.to_thread(journal.control)
        report = await run_collision(
            schedule,
            actor_functions,
            domain=COLLISION_DOMAIN,
            seed=seed,
            root=Path(root),
            faults=FaultProfile.model_validate(env_version.data.fault_profile),
        )
    _record_local(journal, claim, "collision", report)
    return [("collision", report)], "UNKNOWN"


async def _probe(claim, inputs, journal, environment):
    from .platform_jobs import _contract

    if not isinstance(inputs, dict):
        _reject("Probe inputs must be a locally approved candidate plan mapping")
    version = _version(claim, kind="probe")
    spec = version.data
    limits = claim.execution_manifest.limits
    if spec.max_reference_calls > limits.max_reference_calls:
        _reject("Pinned probe budget exceeds the job's authorized reference-call limit", "budget_exceeded")
    if spec.cleanup_reserve > limits.cleanup_reserve:
        _reject("Pinned probe cleanup allowance exceeds the job's authorized cleanup reserve", "budget_exceeded")
    contract = _contract(claim, inputs)
    raw = inputs.get("probe_candidates")
    if not isinstance(raw, list) or len(raw) != len(spec.candidate_ids):
        _reject("Probe inputs must define one locally approved action plan per pinned candidate case")
    by_case = {str(item.get("case_id")): item for item in raw if isinstance(item, dict)}
    if set(by_case) != {str(value) for value in spec.candidate_ids}:
        _reject("Probe candidate IDs differ from the pinned probe revision", "candidate_mismatch")
    cases = {str(v.id): v for v in claim.execution_manifest.versions if v.kind == "case"}
    candidates, plans = [], {}
    for case_id in spec.candidate_ids:
        case = cases.get(str(case_id))
        item = by_case[str(case_id)]
        if case is None:
            _reject("Pinned probe candidate case is missing", "pinned_input_missing")
        candidate = ProbeCandidate.model_validate(item["candidate"])
        if candidate.id != str(case_id):
            _reject("Probe candidate metadata must use its pinned case ID", "candidate_mismatch")
        plan = ActionPlan.model_validate(item["plan"])
        if not plan.valid() or not {action.operation for action in plan.actions} <= set(contract.operations):
            _reject("Probe action plan exceeds the pinned fidelity operations", "operation_denied")
        if candidate.expected_cost != len(plan.actions) or candidate.max_retries:
            _reject("Probe cost must equal its pinned action count and retries must be disabled", "invalid_probe_cost")
        candidates.append(candidate)
        plans[candidate.id] = plan
    checker_digest = content_hash({"fidelity": contract.digest, "criteria": contract.criteria_digest})
    loop = asyncio.get_running_loop()

    def probe_one(candidate, _attempt):
        coroutine = compare(contract, plans[candidate.id], environment, environment)
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        try:
            result: ComparisonResult = future.result(timeout=claim.execution_manifest.limits.case_timeout_seconds)
        except Exception as error:
            return ProbeOutcome(status="error", detail=f"comparison unavailable: {type(error).__name__}")
        if result.status != "compared" or any(
            side.status in {"uncertain", "error"} for step in result.steps for side in (step.simulator, step.reference)
        ):
            return ProbeOutcome(status="unknown", detail="Comparison evidence is incomplete or uncertain")
        return ProbeOutcome(
            status="finding" if not result.matched else "no_finding",
            finding_id=candidate.id if not result.matched else None,
            detail=(
                "Synthetic local comparison found discrepancies; this is not live-provider evidence"
                if not result.matched
                else "Pinned synthetic comparison found no discrepancies"
            ),
        )

    strategies = [spec.strategy] if spec.max_reference_calls < 2 else ["uniform", "priority"]
    per_strategy_cap = spec.max_reference_calls if len(strategies) == 1 else spec.max_reference_calls // 2
    reports = []
    runs, accounts = {}, {}
    for strategy in strategies:
        plan = plan_probes(
            candidates,
            strategy=strategy,
            seed=spec.seed,
            budget_cap=per_strategy_cap,
            checker_digest=checker_digest,
            cleanup_allowance=spec.cleanup_reserve,
        )
        _record_local(journal, claim, f"probe_plan-{strategy}", plan)
        reports.append(("probe_plan", plan))
        run = await asyncio.to_thread(
            run_probes,
            plan,
            probe_one,
            journal=journal,
            scope=f"{claim.attempt_id}:probe:{strategy}",
            cleanup=None,
            workers=1,
        )
        _record_local(journal, claim, f"probe_run-{strategy}", run)
        reports.append(("probe_run", run))
        runs[strategy] = run
        accounts[strategy] = account(run, ())
    if len(strategies) == 2:
        comparison = compare_probes(accounts["uniform"], accounts["priority"])
        _record_local(journal, claim, "probe_comparison", comparison)
        reports.append(("probe_comparison", comparison))
    # Acceptance is a reviewer decision; no finding is self-accepted.
    return reports, "UNKNOWN"


async def _calibration(claim, inputs, journal):
    if not isinstance(inputs, dict):
        _reject("Calibration inputs must be a locally approved control-set mapping")
    grader = _version(claim, kind="grader").data
    if grader.mode != "deterministic":
        return await _semantic_calibration(claim, inputs, journal, grader)
    check_id = inputs.get("check_version_id")
    if not check_id:
        _reject("Deterministic calibration must pin its evaluator check revision", "pinned_input_missing")
    check_version = _version(claim, kind="check", identifier=check_id)
    if grader.evaluator_digest != evaluator_digest():
        _reject("Pinned evaluator implementation digest differs from this runner", "grader_mismatch")
    try:
        check = parse_check(check_version.data.evaluator_definition(check_version.id))
    except Exception:
        _reject("Pinned evaluator check could not be parsed by this runner", "grader_mismatch")
    raw_controls = inputs.get("controls")
    if not isinstance(raw_controls, list):
        _reject("Calibration needs locally approved labeled observations for each pinned control case")
    controls = [Control.model_validate(item) for item in raw_controls]
    positives = {str(value) for value in grader.positive_control_ids}
    negatives = {str(value) for value in grader.negative_control_ids}
    heldout = {str(value) for value in grader.heldout_control_ids}
    if positives & negatives or heldout - (positives | negatives):
        _reject("Pinned positive, negative and held-out control assignments are inconsistent", "control_set_mismatch")
    expected_ids = positives | negatives
    control_ids = [item.id for item in controls]
    if len(control_ids) != len(set(control_ids)) or set(control_ids) != expected_ids:
        _reject("Calibration controls differ from the grader's pinned control case IDs", "control_set_mismatch")
    if any((item.label == "positive") != (item.id in positives) for item in controls):
        _reject("Control labels disagree with the pinned positive/negative control assignment", "control_set_mismatch")
    if any((item.split == "heldout") != (item.id in heldout) for item in controls):
        _reject("Control splits disagree with the pinned held-out assignments", "control_set_mismatch")
    if control_set_hash(check, controls) != grader.control_set_hash:
        _reject("Deterministic controls differ from the grader's pinned control-set hash", "control_set_mismatch")
    result = calibrate(
        check, controls, max_missed_defects=grader.max_missed_defects, max_false_alarms=grader.max_false_alarms
    )
    if result.control_set_hash != grader.control_set_hash:
        _reject("Deterministic calibration hash differs from the pinned control set", "control_set_mismatch")
    _record_local(journal, claim, "deterministic_calibration", result)
    return [("deterministic_calibration", result)], "UNKNOWN"


class _RecordedReplayProvider:
    """Offline, content-addressed fixture. Replies are never inferred from labels."""

    name = "recorded_replay"
    external = False

    def __init__(self, replies: dict[str, dict[str, str]]):
        self.replies = replies

    def grade(self, request: GraderRequest, *, timeout: float):
        del timeout
        key = content_hash(request.model_dump(mode="json"))
        if key not in self.replies:
            raise LookupError("No recorded reply matches this exact grading request")
        return self.replies[key]


class _OpenAICompatibleProvider:
    """One bounded external JSON-chat provider; credentials are read only from a local env reference."""

    name = "openai_compatible"
    external = True

    def __init__(self, endpoint: str, model: str, token_env: str):
        self.endpoint, self.model, self.token_env = endpoint, model, token_env

    def grade(self, request: GraderRequest, *, timeout: float):
        token = os.environ.get(self.token_env)
        if not token:
            raise RuntimeError("Configured semantic provider credential is unavailable")
        rubric = request.rubric
        user = {
            "observation": request.content.get("observation"),
            "records": request.content.get("records", []),
            "truncated": request.content.get("truncated", False),
            "rubric": {
                "criteria": rubric.criteria,
                "pass_definition": rubric.pass_definition,
                "fail_definition": rubric.fail_definition,
            },
        }
        with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            with client.stream(
                "POST",
                self.endpoint,
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": request.prompt},
                        {"role": "user", "content": canonical_json(user)},
                    ],
                    "response_format": {"type": "json_object"},
                },
            ) as response:
                response.raise_for_status()
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 65536:
                        raise ValueError("Semantic provider response exceeds 64 KiB")
                    chunks.append(chunk)
                body = json.loads(b"".join(chunks))
        raw = body["choices"][0]["message"]["content"]
        if not isinstance(raw, str) or len(raw) > 8192:
            raise ValueError("Semantic provider returned an invalid bounded response")
        from susanoo_evaluator.semantic import GraderReply

        return GraderReply.model_validate(json.loads(raw)).model_dump(mode="json")


async def _semantic_calibration(claim, inputs, journal, grader):
    if grader.provider != "recorded_replay":
        _reject("External grading needs a lease-bound outbound producer; no model request was sent",
                "provider_unavailable")
    if grader.evaluator_digest != evaluator_digest():
        _reject("Pinned evaluator implementation digest differs from this runner", "grader_mismatch")
    recorded = inputs.get("semantic_calibration")
    if not isinstance(recorded, dict):
        _reject("Semantic calibration needs a locally approved provider and control set", "provider_unavailable")
    config = GraderConfig.model_validate(recorded.get("config", {}))
    if config.provider != grader.provider or config.model_id != grader.model:
        _reject("Semantic replay configuration differs from the pinned grader", "grader_mismatch")
    if grader.rubric is not None and config.rubric.criteria != grader.rubric:
        _reject("Semantic replay rubric differs from the pinned grader", "grader_mismatch")
    examples = [LabeledExample.model_validate(item) for item in recorded.get("examples", [])]
    example_ids = [example.id for example in examples]
    expected_ids = {
        str(value)
        for value in (
            *grader.positive_control_ids,
            *grader.negative_control_ids,
            *grader.ambiguous_control_ids,
            *grader.heldout_control_ids,
        )
    }
    if len(example_ids) != len(set(example_ids)) or set(example_ids) != expected_ids:
        _reject("Semantic controls differ from the grader's pinned case IDs", "control_set_mismatch")
    positives = {str(value) for value in grader.positive_control_ids}
    negatives = {str(value) for value in grader.negative_control_ids}
    ambiguous_ids = {str(value) for value in grader.ambiguous_control_ids}
    if positives & negatives or positives & ambiguous_ids or negatives & ambiguous_ids:
        _reject("Pinned semantic control label assignments overlap", "control_set_mismatch")
    heldout = {str(value) for value in grader.heldout_control_ids}
    if any((example.split == "heldout") != (example.id in heldout) for example in examples):
        _reject("Semantic splits disagree with the pinned held-out assignments", "control_set_mismatch")
    # Case IDs pin membership; the locally reviewed control-set hash pins the labels, including
    # held-out-only cases retained for backwards compatibility.
    if any(
        (example.id in positives and example.label != "positive")
        or (example.id in negatives and example.label != "negative")
        or (example.id in ambiguous_ids and example.label != "ambiguous")
        for example in examples
    ):
        _reject("Semantic labels disagree with the pinned control assignment", "control_set_mismatch")
    usable = [example for example in examples if example.labeled_by not in {config.model_id, config.grader_id}]
    if not any(e.label == "positive" for e in usable) or not any(e.label == "negative" for e in usable):
        _reject("Semantic controls need locally reviewed positive and negative examples", "control_set_mismatch")
    replies = recorded.get("replies", {})
    permission = ExternalDataPermission.model_validate(recorded.get("permission", {}))
    provider = None
    if grader.provider == "recorded_replay":
        if not isinstance(replies, dict):
            _reject("Recorded semantic replies must be a content-hash mapping")
        expected_keys = {
            content_hash(build_request(config, example.observation, example.content).model_dump(mode="json"))
            for example in usable
        }
        if set(replies) != expected_keys:
            _reject("Recorded replies must exactly cover usable grading requests by content hash", "replay_mismatch")
        provider = _RecordedReplayProvider(replies)
        permission = ExternalDataPermission()
    elif grader.provider == "openai_compatible":
        provider_config = recorded.get("provider_config")
        if not isinstance(provider_config, dict) or set(provider_config) != {"endpoint", "token_env"}:
            _reject(
                "External semantic provider needs a locally approved endpoint and token environment reference",
                "provider_unavailable",
            )
        endpoint = provider_config.get("endpoint")
        token_env = provider_config.get("token_env")
        parsed = urlsplit(endpoint) if isinstance(endpoint, str) else None
        if (
            not parsed
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            _reject(
                "External semantic provider must use an HTTPS endpoint without embedded credentials",
                "provider_unavailable",
            )
        policy = claim.execution_manifest.policy
        if not policy.external_models_allowed or parsed.hostname not in policy.allowed_hosts:
            _reject("Project policy does not permit this external semantic provider host", "external_data_denied")
        if not isinstance(token_env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", token_env):
            _reject("Semantic provider credential must name a local environment variable", "provider_unavailable")
        if (
            not permission.allowed
            or not permission.granted_by
            or any(not permission.permits(example.observation) for example in usable)
        ):
            _reject(
                "External semantic calibration needs explicit permission for every observation", "external_data_denied"
            )
        if not os.environ.get(token_env):
            _reject("Configured semantic provider credential is unavailable", "provider_unavailable")
        provider = _OpenAICompatibleProvider(endpoint, config.model_id, token_env)
    else:
        _reject("Pinned semantic provider has no locally approved implementation", "provider_unavailable")
    result = calibrate_grader(
        config,
        provider,
        examples,
        thresholds=SemanticThresholds.model_validate(recorded.get("thresholds", {})),
        permission=permission,
    )
    if result.control_set_hash != grader.control_set_hash:
        _reject("Semantic calibration control hash differs from the pinned grader", "control_set_mismatch")
    _record_local(journal, claim, "semantic_calibration", result)
    return [("calibration", result)], "UNKNOWN"


@asynccontextmanager
async def _synthetic_echo_server(*, output_chunk_delay_ms: int, max_message_bytes: int):
    async def handler(socket):
        stream_id = None
        caller_audio: list[bytes] = []
        response_seq = 0
        async for raw in socket:
            try:
                frame = json.loads(raw)
                kind = frame["type"]
            except (ValueError, KeyError, TypeError):
                return
            if kind == "start":
                if frame.get("protocol") != "susanoo-media/1":
                    return
                stream_id = frame.get("stream_id")
            elif kind == "media":
                try:
                    caller_audio.append(base64.b64decode(frame["payload"], validate=True))
                except (KeyError, ValueError, TypeError, binascii.Error):
                    return
            elif kind == "mark" and str(frame.get("name", "")).startswith("caller_turn_end:"):
                if not stream_id:
                    return
                for payload in caller_audio:
                    await socket.send(
                        json.dumps(
                            {
                                "type": "media",
                                "stream_id": stream_id,
                                "seq": response_seq,
                                "payload": base64.b64encode(payload).decode("ascii"),
                            }
                        )
                    )
                    response_seq += 1
                    if output_chunk_delay_ms:
                        await asyncio.sleep(output_chunk_delay_ms / 1000)
                await socket.send(json.dumps({"type": "mark", "stream_id": stream_id, "name": "agent_turn_end"}))
                caller_audio.clear()
            elif kind == "stop":
                return

    server = await websockets.serve(handler, "127.0.0.1", 0, max_size=max_message_bytes, ping_interval=None)
    try:
        port = server.sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()


def _approved_audio_clip(value: Any) -> AudioClip:
    if not isinstance(value, dict):
        _reject("Synthetic media turns need a locally approved audio clip", "invalid_media")
    raw = dict(value)
    data = raw.get("data")
    if isinstance(data, str):
        if len(data) > 2 * 1024 * 1024:
            _reject("Synthetic audio clip exceeds the encoded input limit", "input_limit")
        try:
            raw["data"] = base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error):
            _reject("Synthetic audio clip data must be valid base64", "invalid_media")
    try:
        clip = AudioClip.model_validate(raw)
    except Exception:
        _reject("Synthetic audio clip does not match the approved media schema", "invalid_media")
    if not clip.data:
        _reject("Synthetic audio clips cannot be empty", "invalid_media")
    return clip


async def _voice(claim, inputs, journal, profile):
    voice = _version(claim, kind="voice").data
    if profile.data_mode != "synthetic" or claim.execution_manifest.policy.mode != "synthetic":
        _reject("Synthetic WebSocket voice runs require the locally approved synthetic data mode", "profile_refused")
    if voice.provider != "synthetic":
        _reject("Live voice providers are disabled in this worker; no call was made", "provider_unavailable")
    if not isinstance(inputs, dict):
        _reject("Voice inputs must be a locally approved synthetic media script")
    allowed_input_keys = {"job_kind", "media_script", "agent_chunk_delay_ms", "reference_transcript", "hypothesis",
                          "normalization"}
    if set(inputs) - allowed_input_keys:
        _reject("Voice inputs may not supply captured measurements or unrecognized fields", "invalid_media")
    connection_version = _version(claim, kind="connection")
    connection = connection_version.data
    if connection.family != "I12" or connection.adapter != "websocket_media" or not connection.capabilities.audio:
        _reject(
            "Synthetic voice requires a pinned WebSocket media connection with audio capability", "unapproved_adapter"
        )
    if connection.adapter not in profile.allowed_connection_adapters:
        _reject("WebSocket media adapter is not approved on this runner", "unapproved_adapter")
    if content_hash(connection) not in profile.allowed_connection_hashes:
        _reject("Pinned WebSocket media connection is not locally approved", "unapproved_connection")
    suite = _version(claim, kind="suite").data
    cases = [
        v
        for v in claim.execution_manifest.versions
        if v.kind == "case" and str(v.id) in {str(value) for value in suite.case_version_ids}
    ]
    if not cases or any(case.data.modality != "audio" for case in cases):
        _reject("Synthetic voice jobs require pinned audio cases")
    raw_turns = inputs.get("media_script")
    if not isinstance(raw_turns, list) or not 1 <= len(raw_turns) <= 20:
        _reject("Voice jobs need one to twenty locally approved caller audio turns", "invalid_media")
    turns = []
    total_ms = 0.0
    for raw in raw_turns:
        if not isinstance(raw, dict) or set(raw) - {"label", "clip", "barge_in_after_ms", "expect_agent_reply"}:
            _reject("Voice media scripts accept only label, clip and bounded turn controls", "invalid_media")
        clip = _approved_audio_clip(raw.get("clip"))
        total_ms += clip.duration_ms
        turns.append(ScriptedAudioTurn.model_validate({**raw, "clip": clip}))
    if total_ms <= 0 or total_ms * 2 > voice.max_duration_seconds * 1000:
        _reject("Synthetic voice media exceeds the pinned duration limit", "input_limit")
    media_byte_limit = claim.execution_manifest.limits.max_output_bytes
    if media_byte_limit < 1024:
        _reject("Voice media needs at least 1 KiB of approved output budget", "output_limit")
    delay_ms = inputs.get("agent_chunk_delay_ms", 0)
    if isinstance(delay_ms, bool) or not isinstance(delay_ms, int) or not 0 <= delay_ms <= 100:
        _reject("Synthetic response pacing must be an integer from zero to 100 ms", "invalid_media")
    reference = None
    if inputs.get("reference_transcript") is not None:
        try:
            reference = ReferenceTranscript.model_validate(inputs["reference_transcript"])
        except Exception:
            _reject("Reference transcript does not match the reviewed transcript schema", "invalid_media")
    hypothesis = inputs.get("hypothesis")
    if hypothesis is not None and (not isinstance(hypothesis, str) or len(hypothesis) > 100_000):
        _reject("Locally approved transcript hypothesis is invalid or too large", "invalid_media")
    try:
        normalization = NormalizationPolicy.model_validate(inputs.get("normalization", {}))
    except Exception:
        _reject("Transcript normalization policy is invalid", "invalid_media")

    attempt = str(claim.attempt_id)
    if journal.cancel.requested:
        _reject("Voice job was cancelled before media dispatch", "cancelled")
    await asyncio.to_thread(journal.control)
    attempt_scope = f"{attempt}:agent"
    step_id = f"voice-{uuid4().hex}"
    request_digest = content_hash(
        {
            "connection": connection_version.content_hash,
            "turns": [
                {
                    "label": turn.label,
                    "duration_ms": turn.clip.duration_ms,
                    "clip_hash": hashlib.sha256(turn.clip.data).hexdigest(),
                    "barge_in_after_ms": turn.barge_in_after_ms,
                }
                for turn in turns
            ],
        }
    )
    step = await asyncio.to_thread(journal.prepare_step, attempt, step_id, "agent_invoke", request_digest)
    current_storage = sum(
        path.stat().st_size for path in journal.path.parent.glob(journal.path.name + "*") if path.is_file()
    )
    attempt_media_root = journal.path.parent / "media" / attempt
    if attempt_media_root.exists():
        current_storage += sum(path.stat().st_size for path in attempt_media_root.rglob("*") if path.is_file())
    # Every caller turn is echoed by the local synthetic agent, and WAV storage decodes each
    # mu-law sample to 16-bit PCM. Reserve a conservative local-only media allowance first.
    estimate = sum((len(turn.clip.transcode("mulaw", 8000).data) * 4) + 88 for turn in turns)
    if current_storage + estimate > claim.execution_manifest.limits.max_storage_bytes:
        await asyncio.to_thread(journal.abandon_step, attempt, step_id, "storage_limit")
        _reject("Synthetic voice recordings exceed the approved local storage budget", "storage_limit")
    if not await asyncio.to_thread(journal.reserve, attempt_scope, "agent", step.operation_id):
        await asyncio.to_thread(journal.abandon_step, attempt, step_id, "budget_exceeded")
        _reject("Voice job exhausted the approved invocation budget", "budget_exceeded")
    await asyncio.to_thread(journal.mark_dispatching, attempt, step_id, scope=attempt_scope)

    adapter, media_task = None, None
    try:
        async with _synthetic_echo_server(
            output_chunk_delay_ms=delay_ms,
            max_message_bytes=min(16 * 1024 * 1024, media_byte_limit),
        ) as endpoint:
            adapter = WebSocketMediaAdapter(
                MediaSocketConnection(
                    url=endpoint,
                    codec="mulaw",
                    sample_rate=8000,
                    chunk_ms=20,
                    pacing="realtime" if any(t.barge_in_after_ms is not None for t in turns) else "fast",
                    agent_turn_timeout_seconds=min(15.0, claim.execution_manifest.limits.invocation_timeout_seconds),
                    session_timeout_seconds=min(
                        float(voice.max_duration_seconds), claim.execution_manifest.limits.case_timeout_seconds
                    ),
                    max_total_bytes=min(16 * 1024 * 1024, media_byte_limit),
                )
            )
            deadline = datetime.now(UTC) + timedelta(seconds=claim.execution_manifest.limits.case_timeout_seconds)
            media_task = asyncio.create_task(
                adapter.run(turns, session_id=f"syn-{uuid4().hex[:16]}", deadline=deadline)
            )
            while not media_task.done():
                if journal.cancel.requested:
                    await adapter.cancel("synthetic-media-session")
                    break
                try:
                    await asyncio.wait_for(asyncio.shield(media_task), timeout=0.1)
                except TimeoutError:
                    pass
            session = await media_task
        await asyncio.to_thread(
            journal.record_response,
            attempt,
            step_id,
            dispatch_state="acknowledged",
            side_effect_state="none_expected",
            response={"status": session.status, "evidence": session.evidence, "sent_chunks": session.sent_chunks},
        )
    except BaseException as error:
        if journal.step(attempt, step_id).dispatch_state == "dispatching":
            await asyncio.to_thread(
                journal.record_response,
                attempt,
                step_id,
                dispatch_state="uncertain",
                side_effect_state="requires_observation",
                error=type(error).__name__,
            )
        raise
    finally:
        if media_task is not None and not media_task.done():
            if adapter is not None:
                await adapter.cancel("synthetic-media-session")
            media_task.cancel()
            await asyncio.gather(media_task, return_exceptions=True)

    store = MediaStore(StoragePolicy(root=attempt_media_root, upload_allowed=False, data_location="local_only"))
    stored = []
    for turn in session.turns:
        if not turn.audio:
            continue
        clip = AudioClip(
            codec=turn.codec,
            sample_rate=turn.sample_rate,
            data=turn.audio,
            metadata={"source": "synthetic_websocket", "direction": turn.direction},
        )
        item = store.store(clip, f"turn-{turn.index}-{turn.direction}", now=datetime.now(UTC))
        stored.append({"sha256": item.sha256, "size_bytes": item.size_bytes, "uploadable": item.uploadable})
    report = audio_metrics(
        "audio",
        boundaries=session.boundaries(),
        reference=reference,
        hypothesis=hypothesis if session.evidence == "complete" else None,
        policy=normalization,
    )
    if session.evidence != "complete":
        from susanoo_sdk import Unavailable

        report = report.model_copy(
            update={
                "response_latency": Unavailable(
                    reason="media session was incomplete; complete response timing unavailable"
                )
            }
        )
    _record_local(
        journal,
        claim,
        "voice_session",
        {
            "provider": "synthetic_local_websocket_echo",
            "connection_status": "contract_tested",
            "live_verified": False,
            "session_id": session.session_id,
            "stream_id": session.stream_id,
            "status": session.status,
            "evidence": session.evidence,
            "sent_chunks": session.sent_chunks,
            "resent_chunks": session.resent_chunks,
            "events": [event.kind for event in session.events],
            "stored_media": stored,
        },
    )
    _record_local(journal, claim, "media", report)
    return [("media", report)], "UNKNOWN"


async def produce(claim, inputs, *, journal, environment, profile, agent_factory=None):
    """Produce typed reports for locally authorized science jobs; never calls external providers."""
    if not isinstance(inputs, dict) or inputs.get("job_kind") != claim.kind:
        _reject("Science inputs must be locally approved for this exact job kind", "profile_refused")
    if claim.kind == "collision":
        return await _collision(claim, inputs, journal, profile)
    if claim.kind == "probe":
        return await _probe(claim, inputs, journal, environment)
    if claim.kind == "calibration":
        return await _calibration(claim, inputs, journal)
    if claim.kind == "voice":
        return await _voice(claim, inputs, journal, profile)
    _reject(f"No science producer is registered for job kind {claim.kind!r}", "unsupported_job")
