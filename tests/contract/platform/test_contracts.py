import json
from pathlib import Path
from uuid import uuid4

import pytest
from hypothesis import given, strategies as st
from pydantic import ValidationError
from susanoo_contracts import (
    ConnectionSpec,
    RunLimits,
    ScriptStep,
    SuiteSpec,
    VersionCreate,
    canonical_json,
    content_hash,
)
from susanoo_contracts import models


def test_capabilities_default_to_unavailable():
    connection = ConnectionSpec(family="I13", adapter="import")
    assert not connection.capabilities.invoke
    assert not connection.capabilities.observe
    with pytest.raises(ValidationError):
        ConnectionSpec(family="I03", adapter="http", token="secret-canary")


def test_canonical_examples_and_hash_vectors_validate():
    path = Path(__file__).resolve().parents[3] / "packages/contracts/schema/examples.json"
    packet = json.loads(path.read_text(encoding="utf-8"))
    for example in packet["examples"]:
        value = getattr(models, example["model"]).model_validate(example["payload"])
        assert value.model_dump(mode="json") == example["payload"]
    for vector in packet["hash_vectors"]:
        assert canonical_json(vector["value"]) == vector["canonical_json"]
        assert content_hash(vector["value"]) == vector["sha256"]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_canonical_hash_rejects_non_json_numbers(bad):
    with pytest.raises(ValueError):
        content_hash({"value": bad})


@given(st.dictionaries(st.text(max_size=20), st.integers(min_value=-(2**53 - 1), max_value=2**53 - 1), max_size=20))
def test_canonical_hash_is_key_order_independent(value):
    assert content_hash(value) == content_hash(dict(reversed(list(value.items()))))


def test_typed_branches_and_limits():
    with pytest.raises(ValidationError):
        ScriptStep(message="hello", when="reply_equals")
    with pytest.raises(ValidationError):
        RunLimits(max_concurrency=4)
    case_id = uuid4()
    with pytest.raises(ValidationError):
        SuiteSpec(case_version_ids=[case_id, case_id])


def test_jcs_matches_javascript_number_serialization():
    assert (
        canonical_json({"one": 1.0, "tiny": 1e-7, "large": 1e20})
        == '{"large":100000000000000000000,"one":1,"tiny":1e-7}'
    )
    with pytest.raises(ValueError):
        content_hash({"unsafe": 2**53})


def test_hash_uses_wire_aliases_for_typed_response_counts():
    wire = {"scheduled": 1, "attempted": 1, "completed": 1, "pass": 0, "fail": 1, "unknown": 0, "errors": 0}
    assert content_hash(models.RunCounts.model_validate(wire)) == content_hash(wire)


def test_version_round_trip_preserves_discriminator_and_unicode():
    value = VersionCreate(
        logical_id="R1",
        title="Hindi review",
        data={"kind": "requirement", "source_clause": "PTP", "expected": "एक सही रिकॉर्ड", "owner": "reviewer"},
    )
    assert VersionCreate.model_validate_json(value.model_dump_json()) == value
    assert "requirement" in json.dumps(VersionCreate.model_json_schema())
