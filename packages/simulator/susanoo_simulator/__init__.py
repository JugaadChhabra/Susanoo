"""Susanoo stateful simulator. Synthetic reference implementations; injected faults are always labelled."""

from .domains import DOMAINS, get_domain
from .engine import Actor, ActionResult, Domain, FixtureSeed, Operation, Simulator, SimulatorError
from .faults import NO_FAULTS, FaultProfile, FaultRule
from .manager import FixtureManager, FixtureTokens

__all__ = [
    "DOMAINS",
    "NO_FAULTS",
    "ActionResult",
    "Actor",
    "Domain",
    "FaultProfile",
    "FaultRule",
    "FixtureManager",
    "FixtureSeed",
    "FixtureTokens",
    "Operation",
    "Simulator",
    "SimulatorError",
    "get_domain",
]
