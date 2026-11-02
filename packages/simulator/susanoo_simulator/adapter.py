"""``FixtureAdapter`` backed by the simulator HTTP service [Spec §10, §14].

The adapter holds the namespace tokens; the agent receives only its tool route. Observation uses
the read-only observer token, advances the simulated clock by the consistency window first, and
reports an outage as ``unavailable`` rather than as empty state. Test switches can force an
observer outage or a cleanup failure.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
from pydantic import SecretStr
from susanoo_contracts import FixtureSpec, content_hash
from susanoo_evaluator import ObservedState
from susanoo_sdk import (
    AdapterError,
    CleanupResult,
    FixtureContext,
    FixtureHandle,
    ObservationQuery,
    Reconciliation,
    StepIntent,
    ToolRoute,
)

from .engine import FixtureSeed


class SimulatorFixtureAdapter:
    def __init__(
        self,
        base_url: str,
        admin_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        observer_outage: bool = False,
        cleanup_failure: bool = False,
        keep_action_logs: bool = False,
        timeout: float = 10.0,
    ):
        self.base_url = base_url
        self._admin_key = admin_key
        self._client = httpx.AsyncClient(base_url=base_url, transport=transport, timeout=timeout)
        self.observer_outage, self.cleanup_failure = observer_outage, cleanup_failure
        self._tokens: dict[str, dict[str, str]] = {}
        self._baselines: dict[str, dict[str, list[dict]]] = {}
        self._operations: dict[str, list[str]] = {}
        self.keep_action_logs = keep_action_logs
        self.action_logs: dict[str, list[dict]] = {}

    def _auth(self, namespace: str, role: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._tokens[namespace][f'{role}_token']}"}

    async def prepare(self, spec: FixtureSpec, context: FixtureContext) -> FixtureHandle:
        if content_hash(spec.recipe) != spec.digest:
            raise AdapterError("fixture_digest_mismatch", "Fixture recipe does not match its approved digest")
        seed = FixtureSeed.model_validate(spec.recipe["seed"])
        body = {"namespace": context.namespace, "seed": spec.recipe["seed"], "faults": spec.recipe.get("faults", {})}
        response = await self._client.post("/fixtures", json=body, headers={"X-Simulator-Admin": self._admin_key})
        if response.status_code != 201:
            raise AdapterError("fixture_prepare_failed", f"Simulator refused the fixture ({response.status_code})")
        self._tokens[context.namespace] = response.json()
        self._operations[context.namespace] = seed.actors["agent"].operations if "agent" in seed.actors else []
        # Seeded kinds are the before-state; kinds absent from the seed start empty.
        baseline = {kind: [] for kind in seed.entities}
        for kind in seed.entities:
            read = await self._client.get(
                f"/fixtures/{context.namespace}/observe/{kind}", headers=self._auth(context.namespace, "observer")
            )
            read.raise_for_status()
            baseline[kind] = read.json()["records"]
        self._baselines[context.namespace] = baseline
        return FixtureHandle(
            namespace=context.namespace,
            seed_digest=seed.digest,
            aliases=dict(seed.aliases),
            prepared_at=datetime.now(UTC),
        )

    def tool_route(self, fixture: FixtureHandle) -> ToolRoute:
        return ToolRoute(
            base_url=self.base_url,
            namespace=fixture.namespace,
            token=SecretStr(self._tokens[fixture.namespace]["agent_token"]),
            operations=self._operations[fixture.namespace],
        )

    async def observe(self, fixture: FixtureHandle, queries: list[ObservationQuery]) -> list[ObservedState]:
        namespace = fixture.namespace
        if self.observer_outage:
            return [self._unavailable(q, "observer outage switch is on (test)") for q in queries]
        try:
            settle = max((q.window_seconds for q in queries), default=0)
            if settle:
                advanced = await self._client.post(
                    f"/fixtures/{namespace}/clock/advance",
                    json={"seconds": settle},
                    headers=self._auth(namespace, "admin"),
                )
                advanced.raise_for_status()
            results = []
            for query in queries:
                read = await self._client.get(
                    f"/fixtures/{namespace}/observe/{query.kind}", headers=self._auth(namespace, "observer")
                )
                if read.status_code != 200:
                    results.append(self._unavailable(query, f"observer returned {read.status_code}"))
                    continue
                baseline = None
                if query.baseline:
                    baseline = self._baselines.get(namespace, {}).get(query.kind, [])
                results.append(
                    ObservedState(
                        name=query.name,
                        source=f"simulator:{query.kind}",
                        availability="available",
                        completeness="complete_for_scope",
                        window_seconds=query.window_seconds,
                        window_elapsed=True,
                        records=read.json()["records"],
                        baseline=baseline,
                    )
                )
            return results
        except httpx.HTTPError as error:
            return [self._unavailable(q, f"observer unreachable: {type(error).__name__}") for q in queries]

    @staticmethod
    def _unavailable(query: ObservationQuery, reason: str) -> ObservedState:
        return ObservedState(
            name=query.name,
            source=f"simulator:{query.kind}",
            availability="unavailable",
            completeness="unknown",
            window_seconds=query.window_seconds,
            window_elapsed=False,
            reason=reason,
        )

    async def reconcile(self, intent: StepIntent, fixture: FixtureHandle) -> Reconciliation:
        """Read back the attempt-scoped action log to learn whether an uncertain step changed state."""
        try:
            read = await self._client.get(
                f"/fixtures/{fixture.namespace}/log", headers=self._auth(fixture.namespace, "observer")
            )
            read.raise_for_status()
        except (httpx.HTTPError, KeyError) as error:
            return Reconciliation(
                operation_id=intent.operation_id,
                state="unresolved",
                finding="indeterminate",
                note=f"Action log unreadable: {type(error).__name__}",
            )
        matched = [entry for entry in read.json()["actions"] if entry.get("operation_id") == intent.operation_id]
        if not matched:
            return Reconciliation(
                operation_id=intent.operation_id, state="unresolved", finding="indeterminate",
                note="No operation-correlated read-back; unrelated namespace activity is not evidence",
            )
        effects = [
            entry
            for entry in matched
            if entry["mutating"]
            and (entry["status"] == "ok" or (entry["status"] == "error" and "failure_after_commit" in entry["faults"]))
        ]
        if effects:
            names = sorted({entry["operation"] for entry in effects})
            return Reconciliation(
                operation_id=intent.operation_id,
                state="resolved",
                finding="effect_present",
                note=f"{len(effects)} operation-correlated committed change(s): {', '.join(names)}",
            )
        return Reconciliation(
            operation_id=intent.operation_id,
            state="resolved",
            finding="effect_absent",
            note="Operation-correlated log records show no committed change",
        )

    async def cleanup(self, fixture: FixtureHandle) -> CleanupResult:
        namespace = fixture.namespace
        if self.cleanup_failure:
            return CleanupResult(state="failed", reason_code="cleanup_switch", detail="cleanup failure switch (test)")
        if namespace not in self._tokens:
            return CleanupResult(state="not_required", reason_code="never_prepared")
        if self.keep_action_logs:
            log = await self._client.get(f"/fixtures/{namespace}/log", headers=self._auth(namespace, "observer"))
            if log.status_code == 200:
                self.action_logs[namespace] = log.json()["actions"]
        try:
            deleted = await self._client.delete(f"/fixtures/{namespace}", headers=self._auth(namespace, "admin"))
        except httpx.HTTPError as error:
            return CleanupResult(state="failed", reason_code="unreachable", detail=type(error).__name__)
        if deleted.status_code not in (204, 404):
            return CleanupResult(state="failed", reason_code=f"http_{deleted.status_code}")
        self._tokens.pop(namespace, None)
        self._baselines.pop(namespace, None)
        return CleanupResult(state="succeeded")

    async def aclose(self) -> None:
        await self._client.aclose()
