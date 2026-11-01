"""Agent-layer repair in isolated worktrees: policy, gate, held-out, base movement [Spec §15, §17; T31, T32].

The customer repo is a tiny synthetic git repo created in tmp_path with a deliberately buggy collections agent
(it divides every promised amount by ten). The fix flips the reduced reproduction (C01) from FAIL to PASS on the
real local simulator and runner; held-out cases run through a separate evaluator that returns only counts.
"""

import importlib.util
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from collections_demo import build_suite
from susanoo_connectors.source import GitSource
from susanoo_contracts import content_hash
from susanoo_runner import Journal, LocalSuite, SimulatorServer, run_suite
from susanoo_runner.agent_repair import (
    GeneratedPatch,
    HeldoutExposureLedger,
    HeldoutSummary,
    IsolatedWorktree,
    LayerPolicy,
    PatchContext,
    PublicationApproval,
    PublicationMarker,
    PublicationRequest,
    RepairError,
    RepairPolicy,
    SourceUploadApproval,
    StaleProposal,
    UnitTestCommand,
    check_patch_policy,
    compare_criteria,
    generate_patch,
    manual_patch,
    prepare_publication,
    reconcile_publication,
    validate_repair,
)
from susanoo_sdk import InProcessAgentAdapter

ROOT = Path(__file__).resolve().parents[2]
SUITE_DATA = build_suite()
SUITE = LocalSuite.model_validate(SUITE_DATA)
HIDDEN = sorted(c["id"] for c in SUITE_DATA["cases"] if c["visibility"] == "hidden")  # C07, C12, C18
C01 = next(c for c in SUITE_DATA["cases"] if c["id"] == "C01")
SIM_DIGEST = content_hash({"simulator": "collections@1.0", "faults": "none"})

BUGGY = "def normalize_amount(amount_minor: int) -> int:\n    return amount_minor // 10  # treats paise as tenths\n"
FIXED = "def normalize_amount(amount_minor: int) -> int:\n    return amount_minor\n"
AGENT = """from collections_demo.agent import CollectionsAgent

from .amounts import normalize_amount


class PromiseAgent(CollectionsAgent):
    name = "customer-promise-agent"

    def _absorb(self, convo, text):
        super()._absorb(convo, text)
        if convo.amount_minor is not None and "amount_normalized" not in convo.notes:
            convo.amount_minor = normalize_amount(convo.amount_minor)
            convo.notes.append("amount_normalized")
"""
UNIT_TEST = """import importlib.util
import pathlib

spec = importlib.util.spec_from_file_location("amounts", pathlib.Path(__file__).parents[1] / "agent" / "amounts.py")
amounts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(amounts)
assert amounts.normalize_amount(500000) == 500000, "normalize_amount must keep minor units"
print("ok")
"""


def git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Synthetic",
        "GIT_AUTHOR_EMAIL": "synthetic@example.invalid",
        "GIT_COMMITTER_NAME": "Synthetic",
        "GIT_COMMITTER_EMAIL": "synthetic@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


def write(root: Path, files: dict[str, str | None]) -> None:
    for rel, text in files.items():
        path = root / rel
        if text is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)


def make_patch(repo: Path, files: dict[str, str | None]) -> str:
    """A git-style diff of ``files`` against HEAD; the upstream tree is restored afterwards."""
    write(repo, files)
    git(repo, "add", "-A")
    diff = git(repo, "diff", "--cached", "--no-ext-diff")
    git(repo, "reset", "--hard", "--quiet")
    return diff


@pytest.fixture
def upstream(tmp_path):
    root = tmp_path / "customer-agent"
    root.mkdir()
    git(root, "init", "--quiet", "-b", "main")
    checks = {"cases": [{"id": "C01", "checks": C01["checks"]}]}
    write(
        root,
        {
            "agent/__init__.py": "",
            "agent/amounts.py": BUGGY,
            "agent/promise_agent.py": AGENT,
            "tests/test_amounts.py": UNIT_TEST,
            "evaluation/checks.json": json.dumps(checks, indent=1) + "\n",
            "scenarios/heldout/README.md": "held-out material lives elsewhere\n",
            "requirements.txt": "httpx\n",
        },
    )
    git(root, "add", ".")
    git(root, "commit", "--quiet", "-m", "buggy agent")
    return root


def source(repo: Path) -> GitSource:
    return GitSource(url=f"file://{repo}", commit=git(repo, "rev-parse", "HEAD").strip(), allow_local=True)


UNIT = UnitTestCommand(argv=(sys.executable, "tests/test_amounts.py"), timeout_seconds=60)


# --- harnesses ---------------------------------------------------------------------------------------------


class FileHarness:
    """Deterministic in-process stand-in: the reduced case fails while the bug is in the tree."""

    def __init__(self, before="FAIL", controls=None):
        self.before, self._controls, self.calls = before, controls or {"C19": "PASS", "C20": "PASS"}, 0

    async def reproduce(self, tree):
        self.calls += 1
        buggy = "// 10" in (tree / "agent" / "amounts.py").read_text()
        return self.before if buggy else "PASS"

    async def controls(self, tree):
        return dict(self._controls)


def load_agent(tree: Path):
    package = f"customer_agent_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        package, tree / "agent" / "__init__.py", submodule_search_locations=[str(tree / "agent")]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[package] = module
    spec.loader.exec_module(module)
    return importlib.import_module(f"{package}.promise_agent").PromiseAgent


class CollectionsHarness:
    """Real runner + simulator; loads the synthetic customer agent from the given tree."""

    def __init__(self, simulator, workdir: Path, repro="C01", controls=("C19", "C20")):
        self.simulator, self.workdir, self.repro, self.control_ids = simulator, workdir, repro, controls

    async def run(self, tree: Path, case_ids: list[str]) -> dict[str, str]:
        agent_cls = load_agent(tree)
        report = await run_suite(
            SUITE,
            agent_factory=lambda: InProcessAgentAdapter(agent_cls()),
            agent_label="customer",
            journal=Journal(self.workdir / f"{uuid.uuid4().hex}.sqlite"),
            simulator_url=self.simulator.base_url,
            simulator_admin_key=self.simulator.admin_key,
            case_ids=case_ids,
        )
        return {a.case_id: a.business_verdict for a in report.attempts}

    async def reproduce(self, tree):
        return (await self.run(tree, [self.repro]))[self.repro]

    async def controls(self, tree):
        return await self.run(tree, list(self.control_ids))


class SeparateHeldoutEvaluator:
    """Runs hidden cases out of the generator's reach and returns only counts."""

    def __init__(self, harness: CollectionsHarness | None = None, overshare=False):
        self.harness, self.overshare = harness, overshare
        self.split_digest = content_hash({"heldout": HIDDEN})

    async def evaluate(self, tree):
        if self.harness is None:
            verdicts = {
                cid: ("FAIL" if "// 10" in (tree / "agent/amounts.py").read_text() else "PASS") for cid in HIDDEN
            }
        else:
            verdicts = await self.harness.run(tree, HIDDEN)
        counts = {
            "split_digest": self.split_digest,
            "passed": sum(v == "PASS" for v in verdicts.values()),
            "failed": sum(v == "FAIL" for v in verdicts.values()),
            "unknown": sum(v == "UNKNOWN" for v in verdicts.values()),
        }
        if self.overshare:
            counts["cases"] = {cid: SUITE_DATA["cases"][0] for cid in HIDDEN}
        return counts


async def gate(upstream, tmp_path, proposal, *, harness=None, heldout=None, ledger=None, **kwargs):
    return await validate_repair(
        source(upstream),
        proposal,
        harness=harness or FileHarness(),
        heldout=heldout or SeparateHeldoutEvaluator(),
        ledger=ledger or HeldoutExposureLedger(),
        unit_tests=UNIT,
        unit_tests_approved_digest=kwargs.pop("approved", UNIT.digest),
        simulator_digest=SIM_DIGEST,
        failing_check_ids=["promise_amount_matches", "promise_created_once"],
        parent=tmp_path,
        **kwargs,
    )


@pytest.fixture(scope="module")
def simulator():
    with SimulatorServer() as server:
        yield server


# --- isolated worktree --------------------------------------------------------------------------------------


def test_worktree_is_isolated_pinned_and_removed(upstream, tmp_path):
    marker = tmp_path / "hook-ran"
    hook = upstream / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    src = source(upstream)
    fix = make_patch(upstream, {"agent/amounts.py": FIXED})
    with IsolatedWorktree(src, parent=tmp_path) as tree:
        assert tree.head() == src.commit
        assert not tree.path.resolve().is_relative_to(ROOT) and tree.path.resolve().is_relative_to(tmp_path)
        assert tree.apply(fix) == 2
        assert (tree.path / "agent/amounts.py").read_text() == FIXED
        root = tree.root
    assert root is not None and not root.exists()
    assert not marker.exists()
    assert (upstream / "agent/amounts.py").read_text() == BUGGY and git(upstream, "status", "--porcelain") == ""
    with pytest.raises(RepairError) as error:
        IsolatedWorktree(GitSource(url=src.url, commit="a" * 40, allow_local=True), parent=tmp_path).__enter__()
    assert error.value.code == "source_unavailable"


# --- policy ------------------------------------------------------------------------------------------------


def test_layer_policy_rejects_out_of_scope_and_unsafe_patches(upstream):
    policy = RepairPolicy()
    checks_text = (upstream / "evaluation/checks.json").read_text()
    cases = {
        "outside_layer": make_patch(upstream, {"evaluation/checks.json": checks_text.replace("critical", "low")}),
        "heldout_path": make_patch(upstream, {"scenarios/heldout/README.md": "leak\n"}),
    }
    for code, diff in cases.items():
        with pytest.raises(RepairError) as error:
            check_patch_policy(diff, "agent", policy)
        assert error.value.code == code
    big = make_patch(upstream, {"agent/big.py": "".join(f"x{i} = {i}\n" for i in range(70))})
    with pytest.raises(RepairError) as error:
        check_patch_policy(big, "agent", policy)
    assert error.value.code == "patch_too_large" and "60-line limit" in str(error.value)
    link = "diff --git a/agent/l b/agent/l\nnew file mode 120000\n--- /dev/null\n+++ b/agent/l\n@@ -0,0 +1 @@\n+/etc\n"
    escape = "--- a/../outside.py\n+++ b/../outside.py\n@@ -1 +1 @@\n-a\n+b\n"
    quoted = 'diff --git "a/agent/\\tx" "b/agent/\\tx"\n--- "a/agent/\\tx"\n+++ "b/agent/\\tx"\n'
    for diff, code in ((link, "link_in_patch"), (escape, "unsafe_path"), (quoted, "unsafe_path")):
        with pytest.raises(RepairError) as error:
            check_patch_policy(diff, "agent", policy)
        assert error.value.code == code


def test_risky_changes_are_flagged_for_review(upstream):
    policy = RepairPolicy(
        policies={
            **RepairPolicy().policies,
            "agent": LayerPolicy(layer="agent", allowed_paths=("agent/", "requirements.txt", "tests/")),
        }
    )
    diff = make_patch(
        upstream,
        {
            "requirements.txt": "httpx\nrequests\n",
            "agent/net.py": "import httpx\nURL = 'https://example.invalid'\npermissions = ['admin']\n",
            "tests/conftest.py": "import pytest\nskip = pytest.mark.skip\n",
        },
    )
    report = check_patch_policy(diff, "agent", policy)
    assert {f.kind for f in report.flags} == {"dependencies", "network_access", "permissions", "test_exclusion"}
    assert report.requires_review
    fix = check_patch_policy(make_patch(upstream, {"agent/amounts.py": FIXED}), "agent", policy)
    assert fix.flags == () and not fix.requires_review and fix.lines_changed == 2


def test_criteria_comparison_detects_weakening():
    check = C01["checks"][1]
    before = {"evaluation/checks.json": json.dumps({"cases": [{"id": "C01", "checks": C01["checks"]}]})}
    dropped = {"evaluation/checks.json": json.dumps({"cases": [{"id": "C01", "checks": C01["checks"][2:]}]})}
    optional = [{**c, "required": False} if c["id"] == check["id"] else c for c in C01["checks"]]
    lowered = [{**c, "severity": "low"} if c["id"] == check["id"] else c for c in C01["checks"]]
    exclusion = {"cases": [{"id": "C01", "checks": C01["checks"]}], "exclusions": ["C01"]}
    diff = compare_criteria(before, dropped)
    assert diff.weakened and "evaluation/checks.json#C01:promise_amount_matches" in diff.removed
    as_json = {"evaluation/checks.json": json.dumps({"cases": [{"id": "C01", "checks": optional}]})}
    assert compare_criteria(before, as_json).made_optional
    as_json = {"evaluation/checks.json": json.dumps({"cases": [{"id": "C01", "checks": lowered}]})}
    assert compare_criteria(before, as_json).severity_lowered
    assert compare_criteria(before, {"evaluation/checks.json": json.dumps(exclusion)}).exclusions_added
    assert compare_criteria(before, {"evaluation/checks.json": None}).weakened  # deleting the file
    same = compare_criteria(before, dict(before))
    assert not same.changed and not same.weakened
    stricter = [{**c, "window_seconds": 5} if c["id"] == check["id"] else c for c in C01["checks"]]
    tight = compare_criteria(
        before, {"evaluation/checks.json": json.dumps({"cases": [{"id": "C01", "checks": stricter}]})}
    )
    assert tight.changed and not tight.weakened and tight.modified


async def test_evaluator_patch_that_removes_a_required_check_is_rejected(upstream, tmp_path):
    data = json.loads((upstream / "evaluation/checks.json").read_text())
    data["cases"][0]["checks"] = [c for c in data["cases"][0]["checks"] if c["id"] != "promise_amount_matches"]
    weaken = make_patch(upstream, {"evaluation/checks.json": json.dumps(data, indent=1) + "\n"})
    proposal = manual_patch("F-1", "evaluator", weaken, "make the gate green")
    report = check_patch_policy(weaken, "evaluator", RepairPolicy())
    assert {f.kind for f in report.flags} == {"evaluation_criteria"}
    with pytest.raises(RepairError) as error:
        await gate(upstream, tmp_path, proposal)
    assert error.value.code == "criteria_weakened" and "promise_amount_matches" in str(error.value)

    data = json.loads((upstream / "evaluation/checks.json").read_text())
    for c in data["cases"][0]["checks"]:
        if c["id"] == "promise_amount_matches":
            c["expected"]["amount_minor"] = 50000  # rewrite the original failure to match the bug
    rewrite = make_patch(upstream, {"evaluation/checks.json": json.dumps(data, indent=1) + "\n"})
    with pytest.raises(RepairError) as error:
        await gate(upstream, tmp_path, manual_patch("F-1", "evaluator", rewrite, "align expectation"))
    assert error.value.code == "original_failure_rewritten"


# --- the gate ---------------------------------------------------------------------------------------------


async def test_manual_patch_flips_reduced_reproduction_on_the_real_runner(upstream, tmp_path, simulator):
    harness = CollectionsHarness(simulator, tmp_path)
    fix = make_patch(upstream, {"agent/amounts.py": FIXED})
    proposal = manual_patch("F-1", "agent", fix, "Keep minor units; the agent divided amounts by ten")
    ledger = HeldoutExposureLedger(threshold=3, path=tmp_path / "exposure.json")
    result = await gate(
        upstream, tmp_path, proposal, harness=harness, heldout=SeparateHeldoutEvaluator(harness), ledger=ledger
    )
    checks = {c.name: c for c in result.checks}
    assert result.outcome == "accepted", result.checks
    assert checks["reproduction_before_repair"].passed and checks["reduced_reproduction_fixed"].passed
    assert checks["valid_controls_preserved"].passed and checks["unit_tests"].passed
    assert result.heldout == HeldoutSummary(
        split_digest=SeparateHeldoutEvaluator().split_digest, passed=3, failed=0, unknown=0
    )
    assert result.heldout_exposures == 1 and json.loads((tmp_path / "exposure.json").read_text())
    pins = result.pins
    assert pins.agent_commit == source(upstream).commit and pins.agent_after != pins.agent_commit
    assert pins.simulator_before == pins.simulator_after == SIM_DIGEST
    assert pins.evaluator_before == pins.evaluator_after and pins.criteria_before == pins.criteria_after
    assert result.base_commit == source(upstream).commit and result.flags == ()


async def test_patch_that_does_not_fix_is_rejected_and_unapproved_command_refused(upstream, tmp_path):
    noop = make_patch(upstream, {"agent/amounts.py": BUGGY.replace("tenths", "tenths (todo)")})
    result = await gate(upstream, tmp_path, manual_patch("F-1", "agent", noop, "comment"))
    failed = {c.name for c in result.checks if not c.passed}
    assert result.outcome == "rejected"
    assert failed == {"unit_tests", "reduced_reproduction_fixed", "heldout_matches"}
    fix = make_patch(upstream, {"agent/amounts.py": FIXED})
    with pytest.raises(RepairError) as error:
        await gate(upstream, tmp_path, manual_patch("F-1", "agent", fix, "fix"), approved="0" * 64)
    assert error.value.code == "command_not_approved"


async def test_non_reproduction_is_inconclusive_and_invalid_controls_refuse(upstream, tmp_path):
    fix = manual_patch("F-1", "agent", make_patch(upstream, {"agent/amounts.py": FIXED}), "fix")
    result = await gate(upstream, tmp_path, fix, harness=FileHarness(before="PASS"))
    assert result.outcome == "inconclusive" and result.heldout is None
    with pytest.raises(RepairError) as error:
        await gate(upstream, tmp_path, fix, harness=FileHarness(controls={"C19": "FAIL"}))
    assert error.value.code == "invalid_controls"


async def test_heldout_returns_counts_only_and_exposure_forces_rotation(upstream, tmp_path):
    fix = manual_patch("F-1", "agent", make_patch(upstream, {"agent/amounts.py": FIXED}), "fix")
    with pytest.raises(RepairError) as error:
        await gate(upstream, tmp_path, fix, heldout=SeparateHeldoutEvaluator(overshare=True))
    assert error.value.code == "heldout_overshared"
    ledger = HeldoutExposureLedger(threshold=2)
    first = await gate(upstream, tmp_path, fix, ledger=ledger)
    second = await gate(upstream, tmp_path, fix, ledger=ledger)
    third = await gate(upstream, tmp_path, fix, ledger=ledger)
    assert (first.outcome, second.outcome) == ("accepted", "accepted")
    assert third.outcome == "inconclusive" and "rotate" in third.checks[-1].detail
    assert ledger.rotation_required(SeparateHeldoutEvaluator().split_digest)


# --- generated proposals ------------------------------------------------------------------------------------


# Built at runtime so the repository secret scanner does not flag the fake key.
CANARY = "sk-" + "ant-" + "abcdefghijklmnop0123456789"


class RecordingProvider:
    def __init__(self, diff, *, off_host=False, name="local-model"):
        self.diff, self.sends_source_off_host, self.name, self.seen = diff, off_host, name, []

    async def propose(self, context: PatchContext) -> GeneratedPatch:
        self.seen.append(context)
        return GeneratedPatch(diff=self.diff, rationale="Stop dividing by ten. token " + CANARY)


def context_from(tree: IsolatedWorktree, policy: RepairPolicy, case=C01) -> PatchContext:
    rules = policy.for_layer("agent")
    return PatchContext(
        finding_id="F-1",
        layer="agent",
        summary="Promise amount is a tenth of what the customer agreed",
        failing_check_ids=("promise_amount_matches",),
        reduced_case=case,
        files=tree.visible_files(rules.allowed_paths, policy.hidden_paths),
        allowed_paths=rules.allowed_paths,
    )


async def test_generated_patch_never_sees_hidden_cases_and_needs_upload_approval(upstream, tmp_path):
    policy = RepairPolicy()
    fix = make_patch(upstream, {"agent/amounts.py": FIXED})
    with IsolatedWorktree(source(upstream), parent=tmp_path) as tree:
        context = context_from(tree, policy)
    assert set(context.files) == {"agent/__init__.py", "agent/amounts.py", "agent/promise_agent.py"}

    local = RecordingProvider(fix)
    proposal = await generate_patch(local, context, hidden_case_ids=HIDDEN)
    assert proposal.origin == "generated" and proposal.provider == "local-model"
    assert "sk-ant" not in proposal.rationale
    seen = json.dumps([c.model_dump(mode="json") for c in local.seen])
    assert all(cid not in seen for cid in HIDDEN) and "heldout" not in seen

    remote = RecordingProvider(fix, off_host=True, name="hosted-model")
    with pytest.raises(RepairError) as error:
        await generate_patch(remote, context, hidden_case_ids=HIDDEN)
    assert error.value.code == "source_upload_not_approved" and remote.seen == []
    wrong = SourceUploadApproval(project_id="p", approved_by="owner", provider="other")
    with pytest.raises(RepairError):
        await generate_patch(remote, context, hidden_case_ids=HIDDEN, upload_approval=wrong)
    ok = SourceUploadApproval(project_id="p", approved_by="owner", provider="hosted-model")
    assert (await generate_patch(remote, context, hidden_case_ids=HIDDEN, upload_approval=ok)).diff == fix

    hidden_case = next(c for c in SUITE_DATA["cases"] if c["id"] == "C07")
    leaky = context.model_copy(update={"reduced_case": hidden_case})
    probe = RecordingProvider(fix)
    with pytest.raises(RepairError) as error:
        await generate_patch(probe, leaky, hidden_case_ids=HIDDEN)
    assert error.value.code == "hidden_case_exposure" and probe.seen == []
    smuggled = context.model_copy(update={"summary": "compare with C12 behaviour"})
    with pytest.raises(RepairError):
        await generate_patch(probe, smuggled, hidden_case_ids=HIDDEN)
    marker = context.model_copy(update={"summary": "expected 999999 minor"})
    with pytest.raises(RepairError):
        await generate_patch(probe, marker, hidden_case_ids=HIDDEN, hidden_markers=["999999"])
    assert probe.seen == []

    result = await gate(upstream, tmp_path, proposal)
    assert result.outcome == "accepted" and result.origin == "generated"


# --- base movement and publication ---------------------------------------------------------------------------


class GitBase:
    def __init__(self, repo):
        self.repo = repo

    async def current_base(self):
        return git(self.repo, "rev-parse", "main").strip()


class Markers:
    def __init__(self):
        self.created: dict[str, PublicationMarker] = {}

    async def find_marker(self, key):
        return self.created.get(key)


def approval(proposal, validation):
    return PublicationApproval(
        reviewer="reviewer@example.invalid",
        patch_digest=proposal.patch_digest,
        evidence_digest=validation.evidence_digest,
    )


async def test_publication_needs_approval_and_handles_base_movement(upstream, tmp_path):
    fix = manual_patch("F-1", "agent", make_patch(upstream, {"agent/amounts.py": FIXED}), "fix")
    validation = await gate(upstream, tmp_path, fix)
    base = GitBase(upstream)
    with pytest.raises(RepairError) as error:
        await prepare_publication(fix, validation, repository="acme/agent", approval=None, base=base)
    assert error.value.code == "publication_not_authorized"
    request = await prepare_publication(
        fix, validation, repository="acme/agent", approval=approval(fix, validation), base=base
    )
    assert isinstance(request, PublicationRequest)
    assert request.draft is True and request.auto_merge is False
    assert request.base_commit == validation.base_commit and request.patch == fix.diff
    assert request.validation_evidence_digest == validation.evidence_digest

    rejected = validation.model_copy(update={"outcome": "rejected"})
    with pytest.raises(RepairError) as error:
        await prepare_publication(fix, rejected, repository="acme/agent", approval=approval(fix, rejected), base=base)
    assert error.value.code == "not_validated"

    write(upstream, {"README.md": "moved\n"})
    git(upstream, "add", ".")
    git(upstream, "commit", "--quiet", "-m", "base moved")
    moved = await base.current_base()
    stale = await prepare_publication(
        fix, validation, repository="acme/agent", approval=approval(fix, validation), base=base
    )
    assert isinstance(stale, StaleProposal) and stale.current_base == moved
    assert stale.validated_base == validation.base_commit

    async def revalidate(commit):
        return await validate_repair(
            GitSource(url=f"file://{upstream}", commit=commit, allow_local=True),
            fix,
            harness=FileHarness(),
            heldout=SeparateHeldoutEvaluator(),
            ledger=HeldoutExposureLedger(),
            unit_tests=UNIT,
            unit_tests_approved_digest=UNIT.digest,
            simulator_digest=SIM_DIGEST,
            parent=tmp_path,
        )

    fresh = await revalidate(moved)
    request2 = await prepare_publication(
        fix, validation, repository="acme/agent", approval=approval(fix, fresh), base=base, revalidate=revalidate
    )
    assert isinstance(request2, PublicationRequest) and request2.base_commit == moved
    assert request2.publication_key != request.publication_key


async def test_duplicate_publication_reconciles_by_provider_marker(upstream, tmp_path):
    fix = manual_patch("F-1", "agent", make_patch(upstream, {"agent/amounts.py": FIXED}), "fix")
    validation = await gate(upstream, tmp_path, fix)
    args = {"repository": "acme/agent", "approval": approval(fix, validation), "base": GitBase(upstream)}
    first = await prepare_publication(fix, validation, **args)
    again = await prepare_publication(fix, validation, **args)
    assert first.publication_key == again.publication_key  # dedupe key is stable
    markers = Markers()
    assert (await reconcile_publication(first, markers)).action == "safe_to_retry"
    # The platform created the draft PR, then crashed before recording it.
    markers.created[first.publication_key] = PublicationMarker(
        publication_key=first.publication_key, pull_request_url="https://github.com/acme/agent/pull/7"
    )
    result = await reconcile_publication(again, markers)
    assert result.action == "already_published" and result.marker.pull_request_url.endswith("/7")
