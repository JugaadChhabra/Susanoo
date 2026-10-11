"""Runner head (code, no model of its own). [PRD › The eight heads of Orochi] (head 5);
[PRD › Testing environments]; [PRD › Contracts (locked) › Scoring rule (decided)]; IMPLEMENTATION_MUAAZ P3.3.

Runs every scenario against the generated agent and scores the answers with `susanoo.scoring`.

* Level 1 (this node, every iteration): in-process in the worker. The agent's prompt (`agent/prompt.md`) and
  policy (`agent/policy.yaml`) are read from the generated repo as TEXT and assembled into the system prompt the
  same way the template's `backend/reviewer.py` does it. The model calls are real (the model is the agent's
  own, `AgentBundle.model`, billed to the run); the tools are the mocks in `sandbox/mocks.py`. Nothing from the
  generated repo is imported or executed on the host.
* Level 2 (`run_level2`, used by the Verifier for its final check): the whole app in a locked-down container
  through `sandbox/docker.py`; each scenario's diff is POSTed to the app's `/review` endpoint.

Every scenario runs in its own neatlogs trace (a new root trace, not a child of the head span), and
`ScenarioResult.trace_id` is that trace's id, so the Diagnostician can read exactly one scenario run.

GUARDRAIL [PRD › Improvement loop and guardrails]: the Runner scores every split (train, held-out, acceptance),
but nothing it emits names or quotes a held-out or acceptance diff; events carry scores and counts only, and
failing TRAIN scenarios are named by title and id. The traces of held-out runs do contain the held-out diff:
the Diagnostician must drop every result whose scenario is `split == "heldout"` before reading its trace.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx
from opentelemetry import context as otel_context
from pydantic import BaseModel, ValidationError

from susanoo.emit import emit
from susanoo.guards import budget
from susanoo.integrations import llm, tracing
from susanoo.sandbox import docker as docker_sandbox
from susanoo.sandbox.mocks import (
    MAX_TOOL_ROUNDS,
    TOOL_ERROR_PREFIX,
    TOOL_SCHEMAS,
    MockTools,
    WorldState,
    annotate_diff,
    rules_from_policy,
)
from susanoo.schemas import AgentBundle, Finding, IterationMetrics, Rule, Scenario, ScenarioResult
from susanoo.schemas.state import SusanooState
from susanoo.scoring import UnparseableOutput, compute_metrics, parse_findings, score_scenario

log = logging.getLogger(__name__)

HEAD = "runner"
MAX_CONCURRENCY = 4  # scenarios in flight at once
MAX_OUTPUT_TOKENS = 4096  # same as the template's reviewer (a parity test keeps them equal)
MAX_LISTED_FAILURES = 5
MAX_ERROR_CHARS = 300
MAX_TITLE_CHARS = 90

LEVEL2_TIMEOUT_S = 120.0
LEVEL2_READY_TIMEOUT_S = 60.0
LEVEL2_POLL_INTERVAL_S = 1.0
APP_KEY_ENV = "ANTHROPIC_APP_API_KEY"

NO_FAILURES_NOTE = "No failures yet: the scenarios may be too easy."


class RunnerError(Exception):
    """The Runner could not do its job at all (not an agent failure). The message is plain English."""


class _ToolLoopOverflow(Exception):
    """The agent kept asking for tools past the bound."""


# ------------------------------------------------------------------------------------------------ Level 1


@dataclass
class _Meter:
    """Running cost of this Runner pass, shared by the concurrent scenarios, for the budget guard."""

    base: float
    spent: float = 0.0
    exceeded: budget.BudgetExceeded | None = None

    def view(self) -> dict[str, Any]:
        return {"cost_usd": self.base + self.spent}


@dataclass(frozen=True)
class _AgentText:
    """What Level 1 needs from the generated repo, read as text."""

    system_prompt: str | None
    rules: list[Rule]
    problem: str | None = None  # plain English; set when the prompt or policy cannot be used

    @property
    def rule_ids(self) -> set[str]:
        return {r.id for r in self.rules}


def _inside(repo: Path, relative: str) -> Path:
    path = (repo / relative).resolve()
    if not path.is_relative_to(repo.resolve()):
        raise ValueError("the path points outside the agent's repository")
    return path


def assemble_system_prompt(prompt: str, rules: Sequence[Rule]) -> str:
    """The system prompt the template's `Reviewer.system_prompt` builds: prompt.md plus the rules in force."""
    listing = "\n".join(f"- `{r.id}` (default_severity: {r.default_severity}): {r.text}" for r in rules)
    return f"{prompt.rstrip()}\n\n## Rules in force\n\nThese are the only rule ids you may use.\n\n{listing}\n"


def assemble_user_message(diff: str) -> str:
    """The user message the template's `Reviewer.user_message` builds: the line-numbered diff in a data block."""
    body = annotate_diff(diff) or diff
    body = body.replace("</diff>", "<\\/diff>")  # the diff cannot close its own data block
    return f"Review this change.\n\n<diff>\n{body}\n</diff>"


def load_agent_text(repo_path: str | Path, agent: AgentBundle) -> _AgentText:
    """Read the agent's prompt and policy as text. Never imports or executes anything from the repo."""
    repo = Path(repo_path)
    try:
        prompt = _inside(repo, agent.prompt_path).read_text(encoding="utf-8")
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return _AgentText(None, [], f"The agent's prompt file could not be read ({type(exc).__name__}).")
    try:
        policy_text = _inside(repo, agent.policy_path).read_text(encoding="utf-8")
        rules = rules_from_policy(policy_text)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        return _AgentText(None, [], f"The agent's rules file could not be read ({type(exc).__name__}).")
    return _AgentText(assemble_system_prompt(prompt, rules), rules)


class _AppFinding(BaseModel):
    """Mirrors `Finding` in the template's backend/reviewer.py (severity is a plain string there)."""

    file: str
    line: int
    rule_id: str
    severity: str
    message: str


def _extract_json(text: str) -> Any:
    """Same extraction as the template's reviewer: plain JSON, a fenced block, or the outermost {...}."""
    text = text.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(text[start : end + 1])


def app_findings(text: str, rule_ids: Iterable[str]) -> list[Finding]:
    """What the generated app would return for this model text: the template's `Reviewer.parse_findings`.

    Level 1 predicts the app's output, so it applies the app's own post-filter before scoring: JSON is pulled
    out of surrounding text or a fence, malformed items and unknown severities are dropped, and so are findings
    whose rule id is not in policy.yaml. Raises `UnparseableOutput` only where the app itself would fail
    (nothing readable), which scoring rule 8 counts as an error.
    """
    known = set(rule_ids)
    try:
        payload = _extract_json(text)
        raw = payload["findings"] if isinstance(payload, dict) else payload
        if not isinstance(raw, list):
            raise TypeError("findings is not a list")
    except (ValueError, KeyError, TypeError) as exc:
        raise UnparseableOutput(f"the answer is not readable findings ({type(exc).__name__})") from exc
    kept: list[Finding] = []
    dropped = 0
    for item in raw:
        try:
            found = _AppFinding.model_validate(item)
        except ValidationError:
            dropped += 1
            continue
        if found.rule_id not in known or found.severity not in ("block", "warn", "nit"):
            dropped += 1
            continue
        kept.append(Finding.model_validate(found.model_dump()))
    if dropped:
        log.info("The app's filter would drop %d finding(s) (unknown rule id or malformed).", dropped)
    return kept


class _Recording:
    """Wraps an LLM client so the Runner can see the raw response (tool-use blocks) behind `llm.call`."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.last: Any = None
        self.messages = self

    async def create(self, **request: Any) -> Any:
        self.last = await self._inner.messages.create(**request)
        return self.last


def _short(text: str, limit: int = MAX_ERROR_CHARS) -> str:
    text = " ".join(tracing.scrub_text(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def _agent_answer(
    state: Mapping[str, Any],
    scenario: Scenario,
    agent_text: _AgentText,
    meter: _Meter,
    client: Any,
) -> str:
    """Run one scenario's tool-use loop against the agent's model and return its final text.

    Mirrors `Reviewer.review` in the template's backend/reviewer.py call for call (same request shape, same
    history, same stop rule); tests/heads/test_runner.py drives both with one script to keep them identical.
    """
    assert agent_text.system_prompt is not None
    tools = MockTools(diff=scenario.diff, world=WorldState(), rules=agent_text.rules)  # diff + policy text only
    messages: list[dict[str, Any]] = [{"role": "user", "content": assemble_user_message(scenario.diff)}]

    async def create() -> tuple[llm.LLMResult, Any]:
        budget.check(meter.view())  # before EVERY model call
        recording = _Recording(client)
        result = await llm.call(
            HEAD,
            list(messages),
            system=agent_text.system_prompt,
            max_tokens=MAX_OUTPUT_TOKENS,
            client=recording,
            model=state["agent"].model,
            tools=TOOL_SCHEMAS,
        )
        meter.spent += result.cost_usd
        return result, recording.last

    result, response = await create()
    for _ in range(MAX_TOOL_ROUNDS):
        calls = [b for b in response.content if getattr(b, "type", "") == "tool_use"]  # whatever the stop_reason
        if not calls:
            break
        replies: list[dict[str, Any]] = []
        for call in calls:
            output = tools.call_tool(call.name, call.input)
            reply: dict[str, Any] = {"type": "tool_result", "tool_use_id": call.id, "content": output}
            if output.startswith(TOOL_ERROR_PREFIX):
                reply["is_error"] = True
            replies.append(reply)
        messages.append({"role": "assistant", "content": response.content})  # every block, unmodified
        messages.append({"role": "user", "content": replies})
        result, response = await create()
    if any(getattr(b, "type", "") == "tool_use" for b in response.content):
        raise _ToolLoopOverflow
    return result.text


@dataclass
class _Outcome:
    result: ScenarioResult | None  # None only when the scenario was skipped after the budget ran out
    infra_error: bool = False  # the model call itself failed (not the agent's answer)


def _new_root_trace() -> Any:
    """Detach from the head span so the next span starts a new trace of its own."""
    return otel_context.attach(otel_context.Context())


async def _score_level1(
    state: Mapping[str, Any],
    scenario: Scenario,
    agent_text: _AgentText,
    meter: _Meter,
    client: Any,
    iteration: int,
) -> _Outcome:
    token = _new_root_trace()
    try:
        with tracing.span(
            "runner.scenario", kind="AGENT", scenario_id=scenario.id, split=scenario.split, iteration=iteration, level=1
        ):
            ids = tracing.current_ids()
            if ids is None:
                log.warning("No trace id for scenario %s (tracing returned none); using an empty id.", scenario.id)
            trace_id = ids[0] if ids else ""

            def scored(findings: Any = None, error: str | None = None) -> ScenarioResult:
                return score_scenario(
                    scenario,
                    findings,
                    policy_rule_ids=agent_text.rule_ids,
                    iteration=iteration,
                    trace_id=trace_id,
                    error=error,
                )

            if agent_text.system_prompt is None:
                return _Outcome(scored(error=agent_text.problem or "The agent could not be loaded."))
            if meter.exceeded is not None:
                return _Outcome(None)
            try:
                text = await _agent_answer(state, scenario, agent_text, meter, client)
            except budget.BudgetExceeded as exc:
                meter.exceeded = exc
                return _Outcome(None)
            except llm.LLMRefusal:
                return _Outcome(scored(error="The agent's model declined to answer this scenario."))
            except _ToolLoopOverflow:
                return _Outcome(
                    scored(error=f"The agent kept calling tools and never answered (limit {MAX_TOOL_ROUNDS} rounds).")
                )
            except Exception as exc:
                log.error("model call failed for scenario %s: %s", scenario.id, type(exc).__name__)
                return _Outcome(
                    scored(error=f"The agent's model call failed: {type(exc).__name__}. {_short(str(exc))}"),
                    infra_error=True,
                )
            try:
                findings = app_findings(text, agent_text.rule_ids)
            except UnparseableOutput as exc:
                return _Outcome(scored(error=f"The agent's answer could not be read: {_short(str(exc))}"))
            return _Outcome(scored(findings))
    finally:
        otel_context.detach(token)


async def run_level1(
    state: Mapping[str, Any], scenarios: Sequence[Scenario], *, iteration: int | None = None
) -> tuple[list[ScenarioResult], float]:
    """Run scenarios at Level 1. Returns (results in input order, cost of the model calls in USD).

    The caller MUST add the returned cost to `state["cost_usd"]` (the `run` node does). Raises
    `budget.BudgetExceeded` if the run's cost cap is reached (the cost of that partial pass is then lost),
    and `RunnerError` if every model call failed.
    """
    iteration = state.get("iteration", 0) if iteration is None else iteration
    agent_text = load_agent_text(state["repo_path"], state["agent"])
    meter = _Meter(base=float(state.get("cost_usd", 0.0)))
    client = llm.default_client()
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

    async def one(scenario: Scenario) -> _Outcome:
        async with semaphore:
            return await _score_level1(state, scenario, agent_text, meter, client, iteration)

    outcomes = await asyncio.gather(*(one(s) for s in scenarios))
    if meter.exceeded is not None:
        raise meter.exceeded
    if outcomes and all(o.infra_error for o in outcomes):
        raise RunnerError(
            "None of the scenarios could be run because the agent's model did not answer. "
            f"First problem: {outcomes[0].result.error if outcomes[0].result else 'unknown'}"
        )
    return [o.result for o in outcomes if o.result is not None], meter.spent


# ------------------------------------------------------------------------------------------------ Level 2


class SandboxLike(Protocol):
    def build_image(self, repo_path: str | Path, tag: str) -> str: ...
    def start_app(self, run_id: str, image: str, *, env: Mapping[str, str]) -> Any: ...
    def stop_app(self, handle: Any) -> None: ...


def level2_env(connectors: Iterable[str], environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The env for the container: the app's Anthropic key plus the connectors' env vars that are set.

    Values come from the host environment (the vault fills it in later). Only names that are set are passed.
    """
    from susanoo.heads import agent_smith  # lazy: keeps this module light for Level 1 only users

    environ = os.environ if environ is None else environ
    names = [APP_KEY_ENV]
    for env_names in agent_smith.env_var_names_for(list(connectors)).values():
        names.extend(n for n in env_names if n not in names)
    return {n: environ[n] for n in names if environ.get(n)}


async def _wait_ready(http: httpx.AsyncClient, base_url: str, timeout_s: float, interval_s: float) -> None:
    waited = 0.0
    while True:
        try:
            reply = await http.get(f"{base_url}/health")
            if reply.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        if waited >= timeout_s:
            raise RunnerError("The agent's app did not start answering in time.")
        await asyncio.sleep(interval_s)
        waited += interval_s


async def _score_level2(
    http: httpx.AsyncClient,
    base_url: str,
    scenario: Scenario,
    rule_ids: set[str],
    iteration: int,
) -> ScenarioResult:
    token = _new_root_trace()
    try:
        with tracing.span(
            "runner.scenario", kind="AGENT", scenario_id=scenario.id, split=scenario.split, iteration=iteration, level=2
        ):
            ids = tracing.current_ids()
            trace_id = ids[0] if ids else ""

            def scored(findings: Any = None, error: str | None = None) -> ScenarioResult:
                return score_scenario(
                    scenario, findings, policy_rule_ids=rule_ids, iteration=iteration, trace_id=trace_id, error=error
                )

            try:
                reply = await http.post(f"{base_url}/review", json={"diff": scenario.diff})
            except httpx.HTTPError as exc:
                return scored(error=f"The app could not be reached: {type(exc).__name__}.")
            if reply.status_code != 200:
                try:
                    detail = str(reply.json().get("detail", ""))
                except (ValueError, AttributeError):
                    detail = ""
                return scored(error=f"The app answered with status {reply.status_code}. {_short(detail)}".strip())
            try:
                return scored(parse_findings(reply.json()))
            except (UnparseableOutput, ValueError) as exc:
                return scored(error=f"The app's answer could not be read: {_short(str(exc))}")
    finally:
        otel_context.detach(token)


async def _complete(task: asyncio.Future[Any]) -> Any:
    """Await `task` to completion even if this coroutine is cancelled meanwhile, then re-raise the cancellation.

    Used for the blocking sandbox calls (start and stop): a cancelled run must not leave a container behind.
    """
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return result


async def run_level2(
    state: Mapping[str, Any],
    scenarios: Sequence[Scenario],
    *,
    sandbox: SandboxLike | None = None,
    http_client: httpx.AsyncClient | None = None,
    iteration: int | None = None,
    ready_timeout_s: float = LEVEL2_READY_TIMEOUT_S,
    poll_interval_s: float = LEVEL2_POLL_INTERVAL_S,
) -> list[ScenarioResult]:
    """Run scenarios against the whole app in a container (the Verifier's final check). [PRD › Testing environments]

    Builds and starts the generated app, POSTs each scenario's diff to its `/review`, scores the findings, and
    always stops the container, even on failure. Env values are passed to the container and never logged.
    """
    sandbox = sandbox or docker_sandbox  # type: ignore[assignment]
    assert sandbox is not None
    iteration = state.get("iteration", 0) if iteration is None else iteration
    env = level2_env(state.get("connectors", []))
    if APP_KEY_ENV not in env:
        raise RunnerError(f"The Level 2 check needs {APP_KEY_ENV} to be set for the agent's app, and it is not.")
    agent_text = load_agent_text(state["repo_path"], state["agent"])
    if agent_text.system_prompt is None:
        raise RunnerError(agent_text.problem or "The agent could not be loaded.")
    rule_ids = agent_text.rule_ids
    run_id = state["run_id"]
    budget.check(state)

    handle: Any = None
    primary: BaseException | None = None
    own_http = http_client is None
    http = http_client or httpx.AsyncClient(timeout=LEVEL2_TIMEOUT_S)
    try:
        image = await asyncio.to_thread(sandbox.build_image, state["repo_path"], f"susanoo-app-{run_id}:level2")
        log.info("Level 2: starting the app (env names: %s)", sorted(env))
        start = asyncio.ensure_future(asyncio.to_thread(sandbox.start_app, run_id, image, env=env))
        try:
            handle = await _complete(start)
        except asyncio.CancelledError:
            # The thread cannot be interrupted: _complete waited for it, so the container (if any) exists.
            if start.done() and not start.cancelled() and start.exception() is None:
                handle = start.result()
            raise
        base_url = str(handle.internal_url).rstrip("/")
        await _wait_ready(http, base_url, ready_timeout_s, poll_interval_s)
        semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

        async def one(scenario: Scenario) -> ScenarioResult:
            async with semaphore:
                return await _score_level2(http, base_url, scenario, rule_ids, iteration)

        return list(await asyncio.gather(*(one(s) for s in scenarios)))
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            if handle is not None:
                try:
                    await _complete(asyncio.ensure_future(asyncio.to_thread(sandbox.stop_app, handle)))
                except Exception:
                    log.exception("Level 2: could not stop the app container for run %s", run_id)
                    if primary is None:
                        raise
        finally:
            if own_http:
                await http.aclose()


# ------------------------------------------------------------------------------------------------ the node


def _clip(text: str, limit: int = MAX_TITLE_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _summary(scenarios: Sequence[Scenario], results: Sequence[ScenarioResult]) -> tuple[str, dict[str, Any]]:
    """Plain-English summary and payload. Names failing TRAIN scenarios only; other splits are counts."""
    by_id = {s.id: s for s in scenarios}
    failed = [r for r in results if not r.passed]
    if not failed:
        return f"All {len(results)} scenarios pass.", {"failed": [], "total": len(results)}

    train_failed = [r for r in failed if by_id[r.scenario_id].split == "train"]
    parts = [f"{len(failed)} of {len(results)} scenarios failed."]
    if train_failed:
        named = "; ".join(
            f"{_clip(by_id[r.scenario_id].title)} ({r.scenario_id})" for r in train_failed[:MAX_LISTED_FAILURES]
        )
        more = len(train_failed) - MAX_LISTED_FAILURES
        parts.append(f"Failing practice scenarios: {named}{f' and {more} more' if more > 0 else ''}.")
    other = len(failed) - len(train_failed)
    if other:
        held = sum(1 for r in failed if by_id[r.scenario_id].split == "heldout")
        accept = other - held
        bits = []
        if held:
            bits.append(f"{held} hidden check{'s' if held != 1 else ''}")
        if accept:
            approved = all(
                by_id[r.scenario_id].approved_by_user for r in failed if by_id[r.scenario_id].split == "acceptance"
            )
            noun = "your approved example" if approved else "example scenario from intake"
            bits.append(f"{accept} {noun}{'s' if accept != 1 else ''}")
        parts.append("Also failing: " + " and ".join(bits) + ".")
    payload = {
        "failed": [r.scenario_id for r in train_failed],  # train only; held-out ids are not listed
        "failed_total": len(failed),
        "total": len(results),
    }
    return " ".join(parts), payload


async def run(state: SusanooState) -> dict:
    """Level 1 pass over every non-disputed scenario of every split; appends this iteration's metrics once."""
    with tracing.span(HEAD, run_id=state["run_id"]):
        iteration = state.get("iteration", 0)
        await emit(
            state,
            HEAD,
            "started",
            "Running every scenario against the starting version of the agent…"
            if iteration == 0
            else f"Running every scenario against the improved agent (after round {iteration})…",
        )
        try:
            disputed = set(state.get("disputed_scenario_ids", []))  # type: ignore[call-overload]
            scenarios = [s for s in state.get("scenarios", []) if s.id not in disputed]
            results, spent = await run_level1(state, scenarios, iteration=iteration)
            cost = round(float(state.get("cost_usd", 0.0)) + spent, 6)

            update: dict[str, Any] = {"results": results, "cost_usd": cost}
            metrics: list[IterationMetrics] = list(state.get("metrics", []))
            if not any(m.iteration == iteration for m in metrics):
                new = compute_metrics(results, scenarios, iteration=iteration, cost_usd=cost)
                metrics.append(new)
                update["metrics"] = metrics
                text = (
                    "Baseline score recorded."
                    if len(metrics) == 1
                    else f"Score recorded for round {iteration}: hidden-check F1 {new.heldout_f1:.2f}."
                )
                await emit(state, HEAD, "metrics", text, new.model_dump())

            message, payload = _summary(scenarios, results)
            if iteration == 0 and results and all(r.passed for r in results):
                await emit(state, HEAD, "progress", NO_FAILURES_NOTE)
            await emit(state, HEAD, "finished", message, payload)
            return update
        except budget.BudgetExceeded:
            raise  # the graph ends the run cleanly
        except Exception as exc:
            await emit(state, HEAD, "error", f"The test run failed: {_short(str(exc))}")
            raise


__all__ = [
    "HEAD",
    "NO_FAILURES_NOTE",
    "RunnerError",
    "assemble_system_prompt",
    "assemble_user_message",
    "level2_env",
    "load_agent_text",
    "run",
    "run_level1",
    "run_level2",
]
