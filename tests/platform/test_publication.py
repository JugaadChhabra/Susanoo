import asyncio
import copy
import subprocess
from uuid import uuid4

import pytest
from susanoo_contracts import PatchBundle, content_hash
from susanoo_api.platform.publication import reconcile_before_publish, validate_report
from susanoo_api.platform.database import PlatformError
from susanoo_runner.agent_repair import RepairValidation, StaleProposal


def validation_inputs():
    finding = str(uuid4())
    bundle = PatchBundle(base_commit="a"*40, files=[{"path": "agent.py", "before": "old\n", "after": "new\n"}])
    repair = {"id": uuid4(), "finding_id": finding, "layer": "agent", "base_commit": "a"*40,
              "publication_key": "stable-publication", "approved_by": uuid4()}
    validation = RepairValidation(
        finding_id=finding, layer="agent", origin="manual_upload", base_commit="a"*40,
        patch_digest=content_hash({"diff": bundle.validation_diff(), "layer": "agent"}), outcome="accepted",
        checks=[{"name": name, "passed": True, "detail": "Protocol fixture"} for name in (
            "reproduction_before_repair", "criteria_not_weakened", "unit_tests", "reduced_reproduction_fixed",
            "valid_controls_preserved", "heldout_matches",
        )], flags=[],
        pins={"agent_commit": "a"*40, "agent_after": "d"*40, "simulator_before": "e"*64,
              "simulator_after": "e"*64, "evaluator_before": "f"*64, "evaluator_after": "f"*64,
              "criteria_before": "b"*64, "criteria_after": "b"*64},
        heldout={"split_digest": "c"*64, "passed": 1, "failed": 0, "unknown": 0}, heldout_exposures=1,
    )
    return repair, bundle, validation


@pytest.mark.parametrize("field,value", [
    ("patch_digest", "wrong"), ("base_commit", "d"*40), ("outcome", "inconclusive"), ("checks", []),
    ("heldout", {"split_digest": "c"*64, "passed": 0, "failed": 0, "unknown": 1}),
])
def test_repair_report_requires_exact_patch_base_and_all_gates(field, value):
    repair, bundle, validation = validation_inputs()
    assert validate_report(repair, bundle, {"payload": validation.model_dump()}).outcome == "accepted"
    bad = copy.deepcopy(validation.model_dump())
    bad[field] = value
    with pytest.raises(PlatformError):
        validate_report(repair, bundle, {"payload": bad})


def test_existing_provider_marker_precedes_every_base_check_and_write():
    repair, bundle, validation = validation_inputs()
    class Client:
        def find_publication(self, repo, token, repair_id, publication_key):
            assert publication_key == repair["publication_key"]
            return "https://github.com/synthetic/agent/pull/7"
        def request(self, *args, **kwargs):
            raise AssertionError("Do not inspect/rewrite a moved base after discovering the existing publication")
    url, request = asyncio.run(reconcile_before_publish(
        Client(), "synthetic/agent", "fake", repair, bundle, validation
    ))
    assert url.endswith("/7") and request.draft and not request.auto_merge
    assert request.validation_evidence_digest == validation.evidence_digest


def test_moved_base_returns_typed_stale_proposal_without_publication():
    repair, bundle, validation = validation_inputs()
    class Client:
        def find_publication(self, *args):
            return None
        def request(self, method, path, token):
            assert method == "GET"
            return {"default_branch": "main"} if path == "/repos/synthetic/agent" else {"object": {"sha": "d"*40}}
    result, _ = asyncio.run(reconcile_before_publish(Client(), "synthetic/agent", "fake", repair, bundle, validation))
    assert isinstance(result, StaleProposal)
    assert result.current_base == "d"*40 and result.validated_base == "a"*40


@pytest.mark.parametrize("newline", ["", "\n"])
def test_canonical_validation_diff_applies_to_exact_fixture_bytes(tmp_path, newline):
    before, after = "one" + newline, "two" + newline
    bundle = PatchBundle(base_commit="a"*40, files=[{"path": "agent.py", "before": before, "after": after}])
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    (tmp_path / "agent.py").write_bytes(before.encode())
    subprocess.run(["git", "-c", "core.autocrlf=false", "apply", "-"],
                   input=bundle.validation_diff().encode(), cwd=tmp_path, check=True)
    assert (tmp_path / "agent.py").read_bytes() == after.encode()
