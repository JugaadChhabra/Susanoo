"""HTTP API for the simulator: agent tools, read-only observation, admin lifecycle and an inspector.

Run locally beside the customer runner, never inside the SaaS control plane. Creating a fixture
needs the service admin key given at startup; everything else uses namespace-scoped tokens.
"""

import argparse
import os
import secrets
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .engine import FixtureSeed, SimulatorError
from .faults import NO_FAULTS, FaultProfile
from .manager import FixtureManager, Grant


class CreateFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")
    namespace: str
    seed: FixtureSeed
    faults: FaultProfile = NO_FAULTS
    agent_actor: str = "agent"


class OperationCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=200)


class Advance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seconds: float = Field(ge=0, le=86400 * 30)


def _bearer(authorization: str | None) -> str | None:
    if authorization and authorization.startswith("Bearer "):
        return authorization.removeprefix("Bearer ")
    return None


def create_app(root: Path, admin_key: str) -> FastAPI:
    manager = FixtureManager(root)
    app = FastAPI(title="Susanoo simulator", version="1.0")
    app.state.manager = manager

    def grant(*roles: str):
        def dependency(namespace: str, authorization: str | None = Header(default=None)) -> Grant:
            try:
                manager.get(namespace)
                return manager.authorize(_bearer(authorization), namespace, *roles)
            except KeyError:
                raise HTTPException(404, "Unknown namespace") from None
            except PermissionError:
                raise HTTPException(403, "Token not valid for this namespace and role") from None

        return dependency

    AgentGrant = Annotated[Grant, Depends(grant("agent"))]
    ReadGrant = Annotated[Grant, Depends(grant("observer", "admin"))]
    AdminGrant = Annotated[Grant, Depends(grant("admin"))]

    @app.get("/health")
    def health():
        return {"status": "ok", "namespaces": len(manager.namespaces())}

    @app.post("/fixtures", status_code=201)
    def create(body: CreateFixture, x_simulator_admin: str | None = Header(default=None)):
        if not x_simulator_admin or not secrets.compare_digest(x_simulator_admin, admin_key):
            raise HTTPException(403, "Simulator admin key required")
        try:
            tokens = manager.create(body.namespace, body.seed, body.faults, agent_actor=body.agent_actor)
        except FileExistsError as error:
            raise HTTPException(409, str(error)) from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        return tokens.__dict__

    @app.post("/fixtures/{namespace}/operations/{operation}")
    def call(namespace: str, operation: str, body: OperationCall, who: AgentGrant):
        try:
            result = manager.get(namespace).execute(who.actor, operation, body.request, body.idempotency_key)
        except SimulatorError as error:
            # The agent sees a transport failure; the action log keeps what really happened.
            raise HTTPException(503 if error.code == "unavailable" else 502, error.message) from None
        return result.model_dump()

    @app.get("/fixtures/{namespace}/observe/{kind}")
    def observe(namespace: str, kind: str, who: ReadGrant):
        sim = manager.get(namespace)
        try:
            records = sim.observe(kind)
        except KeyError:
            raise HTTPException(404, f"Unknown entity kind {kind}") from None
        return {"kind": kind, "records": records, "clock": sim.now().isoformat(), "complete_for_scope": True}

    @app.get("/fixtures/{namespace}/log")
    def log(namespace: str, who: ReadGrant):
        return {"actions": manager.get(namespace).action_log()}

    @app.get("/fixtures/{namespace}/inspect")
    def inspect(namespace: str, who: AdminGrant):
        sim = manager.get(namespace)
        return {
            "domain": sim.domain.name,
            "clock": sim.now().isoformat(),
            "digest": sim.digest(),
            "faults": sim.faults.model_dump(),
            "state": sim.state(),
            "actions": sim.action_log(),
        }

    @app.post("/fixtures/{namespace}/snapshot")
    def snapshot(namespace: str, who: AdminGrant):
        return manager.get(namespace).snapshot()

    @app.post("/fixtures/{namespace}/reset")
    def reset(namespace: str, who: AdminGrant):
        sim = manager.get(namespace)
        sim.reset()
        return {"digest": sim.digest()}

    @app.post("/fixtures/{namespace}/clock/advance")
    def advance(namespace: str, body: Advance, who: AdminGrant):
        return {"clock": manager.get(namespace).advance(body.seconds).isoformat()}

    @app.put("/fixtures/{namespace}/faults")
    def faults(namespace: str, body: FaultProfile, who: AdminGrant):
        manager.get(namespace).set_faults(body)
        return body.model_dump()

    @app.delete("/fixtures/{namespace}", status_code=204)
    def delete(namespace: str, who: AdminGrant):
        manager.destroy(namespace)

    return app


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Run the Susanoo simulator service locally")
    parser.add_argument("--root", type=Path, default=Path(".susanoo/simulator"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    admin_key = os.environ.get("SUSANOO_SIMULATOR_ADMIN_KEY")
    if not admin_key:
        raise SystemExit("Set SUSANOO_SIMULATOR_ADMIN_KEY")
    uvicorn.run(create_app(args.root, admin_key), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
