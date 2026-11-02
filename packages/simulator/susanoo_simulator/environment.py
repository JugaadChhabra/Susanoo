"""``EnvironmentAdapter`` over the simulator service, for fixed-action comparison [Spec §14-15].

Used for both the simulator under test and the authorized reference (here: a second simulator
configuration with a different policy/fault profile, clearly labelled synthetic). Each comparison
side gets its own namespace and fresh fixture; nothing is shared between sides.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
from susanoo_contracts import content_hash
from susanoo_sdk import ActionContext, ActionResult, ApprovedAction, SnapshotScope, StateSnapshot

from .engine import FixtureSeed
from .faults import NO_FAULTS, FaultProfile


class SimulatorEnvironment:
    def __init__(self, base_url: str, admin_key: str, *, label: str, timeout: float = 10.0):
        self.label = label
        self._admin_key = admin_key
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)
        self._tokens: dict[str, dict[str, str]] = {}
        self.kinds: dict[str, list[str]] = {}

    async def prepare(self, namespace: str, seed: FixtureSeed, faults: FaultProfile = NO_FAULTS) -> str:
        body = {"namespace": namespace, "seed": seed.model_dump(mode="json"), "faults": faults.model_dump(mode="json")}
        response = await self._client.post("/fixtures", json=body, headers={"X-Simulator-Admin": self._admin_key})
        response.raise_for_status()
        self._tokens[namespace] = response.json()
        inspect = await self._client.get(f"/fixtures/{namespace}/inspect", headers=self._auth(namespace, "admin"))
        inspect.raise_for_status()
        self.kinds[namespace] = sorted(inspect.json()["state"])
        return response.json()["seed_digest"]

    def _auth(self, namespace: str, role: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._tokens[namespace][f'{role}_token']}"}

    async def execute(self, action: ApprovedAction, context: ActionContext) -> ActionResult:
        response = await self._client.post(
            f"/fixtures/{context.namespace}/operations/{action.operation}",
            json={"request": action.request, "idempotency_key": action.idempotency_key},
            headers=self._auth(context.namespace, "agent"),
        )
        if response.status_code >= 500:
            # The write may have committed before the transport failed.
            return ActionResult(status="uncertain", code=f"http_{response.status_code}")
        response.raise_for_status()
        body = response.json()
        return ActionResult(status=body["status"], code=body.get("code"), response=body["response"])

    async def snapshot(self, scope: SnapshotScope) -> StateSnapshot:
        namespace = scope.namespace
        state = {}
        for kind in scope.kinds:
            read = await self._client.get(
                f"/fixtures/{namespace}/observe/{kind}", headers=self._auth(namespace, "observer")
            )
            read.raise_for_status()
            state[kind] = sorted(read.json()["records"], key=lambda row: str(row.get("id")))
        return StateSnapshot(captured_at=datetime.now(UTC), digest=content_hash(state), state=state)

    async def settle(self, namespace: str, seconds: int) -> None:
        if seconds:
            advanced = await self._client.post(
                f"/fixtures/{namespace}/clock/advance",
                json={"seconds": seconds},
                headers=self._auth(namespace, "admin"),
            )
            advanced.raise_for_status()

    async def destroy(self, namespace: str) -> None:
        if namespace in self._tokens:
            await self._client.delete(f"/fixtures/{namespace}", headers=self._auth(namespace, "admin"))
            self._tokens.pop(namespace, None)

    async def aclose(self) -> None:
        for namespace in list(self._tokens):
            await self.destroy(namespace)
        await self._client.aclose()
