"""Sample payloads validate against the frozen models. [PRD › Contracts (locked)]

The JSON files in tests/fixtures/ are also the mock data for frontend screens, so they must stay valid.
"""

import json
from pathlib import Path

import pytest
from pydantic import BaseModel
from susanoo.schemas import Diagnosis, IterationMetrics, RunEvent, Scenario

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

CASES: list[tuple[str, type[BaseModel]]] = [
    ("run_events.json", RunEvent),
    ("scenarios.json", Scenario),
    ("iteration_metrics.json", IterationMetrics),
    ("diagnoses.json", Diagnosis),
]


def load(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text())


@pytest.mark.parametrize(("name", "model"), CASES)
def test_fixture_validates(name: str, model: type[BaseModel]) -> None:
    payloads = load(name)
    assert payloads, f"{name} is empty"
    for payload in payloads:
        model.model_validate(payload)


@pytest.mark.parametrize(("name", "model"), CASES)
def test_fixture_has_no_unknown_fields(name: str, model: type[BaseModel]) -> None:
    """Pydantic ignores extras by default; a typo in a fixture would hide a contract drift."""
    for payload in load(name):
        assert set(payload) <= set(model.model_fields), set(payload) - set(model.model_fields)


@pytest.mark.parametrize(("name", "model"), CASES)
def test_fixture_round_trips(name: str, model: type[BaseModel]) -> None:
    for payload in load(name):
        assert model.model_validate(payload).model_dump() == payload


def test_run_events_are_ordered_and_one_run() -> None:
    events = [RunEvent.model_validate(p) for p in load("run_events.json")]
    assert [e.seq for e in events] == sorted(e.seq for e in events)
    assert len({e.run_id for e in events}) == 1


def test_invalid_payload_is_rejected() -> None:
    with pytest.raises(ValueError):
        RunEvent.model_validate({"run_id": "x", "seq": 1, "head": "nobody", "type": "started"})
    with pytest.raises(ValueError):
        Diagnosis.model_validate({**load("diagnoses.json")[0], "layer": "vibes"})
