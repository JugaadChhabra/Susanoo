"""Namespaced fixture management with role-scoped tokens.

Every case gets its own namespace and SQLite file. Tokens are bound to one namespace and one role:
``agent`` (approved operations as one actor), ``observer`` (read-only) or ``admin`` (reset, clock,
faults, delete). A prompt cannot widen a token; the binding lives here, not in the request.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .domains import get_domain
from .engine import FixtureSeed, Simulator
from .faults import NO_FAULTS, FaultProfile

Role = Literal["agent", "observer", "admin"]
NAMESPACE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


@dataclass(frozen=True)
class Grant:
    namespace: str
    role: Role
    actor: str | None = None


@dataclass(frozen=True)
class FixtureTokens:
    namespace: str
    seed_digest: str
    agent_token: str
    observer_token: str
    admin_token: str


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class FixtureManager:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._sims: dict[str, Simulator] = {}
        self._grants: dict[str, Grant] = {}
        self._lock = threading.Lock()

    def create(
        self, namespace: str, seed: FixtureSeed, faults: FaultProfile = NO_FAULTS, *, agent_actor: str = "agent"
    ) -> FixtureTokens:
        if not NAMESPACE.match(namespace):
            raise ValueError("Namespace must be 1-100 safe characters")
        if agent_actor not in seed.actors:
            raise ValueError(f"Seed defines no actor {agent_actor}")
        with self._lock:
            if namespace in self._sims:
                raise FileExistsError(f"Namespace {namespace} already exists")
            sim = Simulator.create(self.root / f"{namespace}.sqlite", get_domain(seed.domain), seed, faults)
            self._sims[namespace] = sim
            tokens = {role: secrets.token_urlsafe(32) for role in ("agent", "observer", "admin")}
            self._grants[_hash(tokens["agent"])] = Grant(namespace, "agent", agent_actor)
            self._grants[_hash(tokens["observer"])] = Grant(namespace, "observer")
            self._grants[_hash(tokens["admin"])] = Grant(namespace, "admin")
        return FixtureTokens(namespace, seed.digest, tokens["agent"], tokens["observer"], tokens["admin"])

    def get(self, namespace: str) -> Simulator:
        sim = self._sims.get(namespace)
        if sim is None:
            raise KeyError(namespace)
        return sim

    def authorize(self, token: str | None, namespace: str, *roles: Role) -> Grant:
        grant = self._grants.get(_hash(token)) if token else None
        if grant is None or grant.namespace != namespace or grant.role not in roles:
            raise PermissionError("Token is not valid for this namespace and role")
        return grant

    def destroy(self, namespace: str) -> None:
        with self._lock:
            sim = self._sims.pop(namespace, None)
            self._grants = {key: grant for key, grant in self._grants.items() if grant.namespace != namespace}
        if sim is not None:
            sim.destroy()

    def namespaces(self) -> list[str]:
        return sorted(self._sims)
