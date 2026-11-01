from ..engine import Domain
from . import collections, issue_tracker, shared_ledger

DOMAINS: dict[str, Domain] = {
    domain.name: domain for domain in (collections.DOMAIN, issue_tracker.DOMAIN, shared_ledger.DOMAIN)
}


def get_domain(name: str) -> Domain:
    if name not in DOMAINS:
        raise KeyError(f"Unknown simulator domain {name}")
    return DOMAINS[name]
