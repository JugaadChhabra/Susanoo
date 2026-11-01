"""Approved custom environment adapters [Spec §14; deliverable 7].

A customer may plug in their own simulator through a locally installed, versioned adapter. The SaaS never
executes uploaded code: registration takes a factory that is already importable on the runner host, refuses
factories whose source lives under an upload directory, and requires an approval whose digest matches the
exact adapter configuration. The wrapper enforces the approved operation allowlist, observable kinds,
response-size cap and per-call deadline around any object satisfying the simulator environment protocol
(``susanoo_simulator.environment.SimulatorEnvironment``) and the SDK ``EnvironmentAdapter``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator
from susanoo_contracts import content_hash
from susanoo_sdk import ActionContext, ActionResult, ApprovedAction, SnapshotScope, StateSnapshot
from susanoo_sdk.adapters import EnvironmentAdapter

from .engine import FixtureSeed
from .faults import NO_FAULTS, FaultProfile


class CustomAdapterError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@runtime_checkable
class SimulatorEnvironmentProtocol(Protocol):
    """The duck-typed surface fidelity comparison uses (see ``SimulatorEnvironment``)."""

    async def prepare(self, namespace: str, seed: FixtureSeed, faults: FaultProfile = NO_FAULTS) -> str: ...

    async def execute(self, action: ApprovedAction, context: ActionContext) -> ActionResult: ...

    async def snapshot(self, scope: SnapshotScope) -> StateSnapshot: ...

    async def settle(self, namespace: str, seconds: int) -> None: ...

    async def destroy(self, namespace: str) -> None: ...


class CustomAdapterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    version: str = Field(min_length=1, max_length=40)
    entrypoint: str = Field(pattern=r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$", description="installed module:attr")
    operations: tuple[str, ...] = Field(min_length=1)
    read_operations: tuple[str, ...] = ()
    kinds: tuple[str, ...] = Field(min_length=1)
    max_response_bytes: int = Field(default=256 * 1024, ge=1, le=16 * 1024 * 1024)
    timeout_seconds: float = Field(default=10.0, gt=0, le=300)
    validation_status: Literal["unvalidated", "validated"] = "unvalidated"
    reference_tests_digest: str | None = None
    scaffold_digest: str | None = None

    @model_validator(mode="after")
    def _validated_needs_tests(self) -> CustomAdapterConfig:
        if self.validation_status == "validated" and not self.reference_tests_digest:
            raise ValueError("A validated adapter needs reviewed reference tests (reference_tests_digest)")
        if not set(self.read_operations) <= set(self.operations):
            raise ValueError("read_operations must be a subset of operations")
        return self

    @property
    def digest(self) -> str:
        return content_hash(self.model_dump(mode="json"))


class AdapterApproval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    config_digest: str
    approved_by: str = Field(min_length=1)
    rationale: str = Field(min_length=1)


class ApprovedEnvironment:
    """Policy wrapper; injected faults and fidelity status stay labelled by the config."""

    def __init__(self, inner: Any, config: CustomAdapterConfig, approval: AdapterApproval):
        self._inner = inner
        self.config = config
        self.approval = approval
        self.label = f"{config.name}@{config.version}"
        self.validation_status = config.validation_status

    @property
    def digest(self) -> str:
        return self.config.digest

    async def _call(self, coro):
        try:
            return await asyncio.wait_for(coro, timeout=self.config.timeout_seconds)
        except TimeoutError:
            raise CustomAdapterError(
                "adapter_timeout", f"{self.label} exceeded {self.config.timeout_seconds}s"
            ) from None

    def _size_ok(self, payload: Any) -> None:
        size = len(json.dumps(payload, default=str))
        if size > self.config.max_response_bytes:
            raise CustomAdapterError("response_too_large", f"{self.label} returned {size} bytes")

    async def prepare(self, namespace: str, seed: FixtureSeed, faults: FaultProfile = NO_FAULTS) -> str:
        return await self._call(self._inner.prepare(namespace, seed, faults))

    async def execute(self, action: ApprovedAction, context: ActionContext) -> ActionResult:
        if action.operation not in self.config.operations:
            return ActionResult(status="rejected", code="operation_not_approved")
        result = await self._call(self._inner.execute(action, context))
        if not isinstance(result, ActionResult):
            result = ActionResult.model_validate(result)
        self._size_ok(result.response)
        return result

    async def snapshot(self, scope: SnapshotScope) -> StateSnapshot:
        refused = sorted(set(scope.kinds) - set(self.config.kinds))
        if refused:
            raise CustomAdapterError("kind_not_approved", f"Kinds {refused} are not approved for {self.label}")
        snap = await self._call(self._inner.snapshot(scope))
        if not isinstance(snap, StateSnapshot):
            snap = StateSnapshot.model_validate(snap)
        self._size_ok(snap.state)
        return snap

    async def settle(self, namespace: str, seconds: int) -> None:
        await self._call(self._inner.settle(namespace, seconds))

    async def destroy(self, namespace: str) -> None:
        await self._call(self._inner.destroy(namespace))

    async def aclose(self) -> None:
        if hasattr(self._inner, "aclose"):
            await self._inner.aclose()


class CustomAdapterRegistry:
    """Approved adapters by (name, version). Unapproved or changed configurations are refused."""

    def __init__(self, *, upload_roots: Iterable[Path] = ()):
        self._upload_roots = [Path(p).resolve() for p in upload_roots]
        self._entries: dict[tuple[str, str], tuple[CustomAdapterConfig, AdapterApproval, Callable[..., Any]]] = {}

    def register(
        self, config: CustomAdapterConfig, approval: AdapterApproval | None, factory: Callable[..., Any]
    ) -> str:
        if approval is None:
            raise CustomAdapterError("adapter_not_approved", f"{config.name}@{config.version} has no approval")
        if approval.config_digest != config.digest:
            raise CustomAdapterError("approval_mismatch", "The approval does not match this adapter configuration")
        if not callable(factory):
            raise CustomAdapterError("invalid_factory", "The adapter factory is not callable")
        try:
            source = inspect.getsourcefile(factory)
        except (TypeError, OSError):
            source = None  # not from an importable installed module
        if source is None:
            raise CustomAdapterError("invalid_factory", "The adapter factory has no installed source file")
        resolved = Path(source).resolve()
        if any(resolved.is_relative_to(root) for root in self._upload_roots):
            raise CustomAdapterError("uploaded_code", "Adapters from uploaded code are never executed")
        key = (config.name, config.version)
        if key in self._entries and self._entries[key][0].digest != config.digest:
            raise CustomAdapterError("version_conflict", "A different configuration is registered for this version")
        self._entries[key] = (config, approval, factory)
        return config.digest

    def create(self, name: str, version: str, *args: Any, **kwargs: Any) -> ApprovedEnvironment:
        if (name, version) not in self._entries:
            raise CustomAdapterError("adapter_not_registered", f"{name}@{version} is not an approved adapter")
        config, approval, factory = self._entries[(name, version)]
        inner = factory(*args, **kwargs)
        if not isinstance(inner, EnvironmentAdapter) or not isinstance(inner, SimulatorEnvironmentProtocol):
            raise CustomAdapterError("protocol_mismatch", f"{name}@{version} does not implement the environment API")
        return ApprovedEnvironment(inner, config, approval)


__all__ = [
    "AdapterApproval",
    "ApprovedEnvironment",
    "CustomAdapterConfig",
    "CustomAdapterError",
    "CustomAdapterRegistry",
    "SimulatorEnvironmentProtocol",
]
