"""Agent/simulator/evaluator code repair in isolated worktrees [Spec §15 › Repair gate, §17; F06, T31, T32].

Workflow: pin source -> isolated clone + detached worktree -> manual or generated patch -> layer policy
(path allowlist, change cap, risk flags, criterion-weakening) -> validation gate (approved unit-test argv,
reduced repro FAIL before / PASS after, valid controls, held-out through a separate limited evaluator,
exposure counter, separately pinned layers) -> base-movement check -> ``PublicationRequest`` for the
platform. Nothing here pushes, opens a PR or merges; Aarav's platform owns authorized publication.

Reused legacy guards (wrapped, not modified): ``susanoo.guards.layer_allowlist`` (held-out wall and
repo-path normalisation) and ``susanoo.guards.patch_size`` (numstat counting and ``PatchTooLarge``). Git
runs with ``susanoo_connectors.source.GIT_CONFIG`` (hooks, fsmonitor, submodules off) and the clone's
``info/attributes`` disables every filter, as in source intake.
"""

from __future__ import annotations

import asyncio
import json
import os
import re

try:
    import resource
except ImportError:
    resource = None
import shutil
import subprocess
import signal
import threading
import time
import tempfile
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from susanoo.guards import layer_allowlist, patch_size
from susanoo_connectors.source import GIT_CONFIG, GitSource, SourceError, acquire_git, tree_digest
from susanoo_contracts import content_hash

from susanoo_evaluator import evaluator_digest, parse_check

Layer = Literal["agent", "simulator", "evaluator"]
Verdict = Literal["PASS", "FAIL", "UNKNOWN"]
FlagKind = Literal["evaluation_criteria", "test_exclusion", "permissions", "network_access", "dependencies"]
COMMIT = re.compile(r"^[0-9a-f]{40}$")
_LINK_MODES = ("120000", "160000")  # symlink, submodule (same rule as the legacy Fixer)


class RepairError(Exception):
    """Plain-English refusal; ``code`` is machine-readable."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- policy -----------------------------------------------------------------------------------------------


class LayerPolicy(Model):
    """Paths a repair of one layer may touch and how big it may be [Spec §17]."""

    layer: Layer
    allowed_paths: tuple[str, ...] = Field(min_length=1)
    max_lines_changed: int = Field(default=patch_size.MAX_LINES_CHANGED, ge=1)
    max_files: int = Field(default=10, ge=1)


DEFAULT_POLICIES: dict[Layer, LayerPolicy] = {
    "agent": LayerPolicy(layer="agent", allowed_paths=("agent/", "src/", "prompts/")),
    "simulator": LayerPolicy(layer="simulator", allowed_paths=("simulator/", "fixtures/")),
    "evaluator": LayerPolicy(layer="evaluator", allowed_paths=("evaluation/", "checks/")),
}


class RepairPolicy(Model):
    policies: dict[str, LayerPolicy] = Field(default_factory=lambda: dict(DEFAULT_POLICIES))
    check_files: tuple[str, ...] = ("evaluation/checks.json",)
    hidden_paths: tuple[str, ...] = ("scenarios/heldout/", "evaluation/heldout/")

    def for_layer(self, layer: Layer) -> LayerPolicy:
        if layer not in self.policies:
            raise RepairError("unknown_layer", f"No repair policy for layer {layer!r}")
        return self.policies[layer]


class RiskFlag(Model):
    kind: FlagKind
    path: str
    evidence: str


_DEPENDENCY_FILES = re.compile(
    r"(^|/)(pyproject\.toml|setup\.(py|cfg)|requirements[^/]*\.txt|Pipfile(\.lock)?|uv\.lock|poetry\.lock|"
    r"package(-lock)?\.json|yarn\.lock|pnpm-lock\.yaml|go\.(mod|sum)|Cargo\.(toml|lock)|Gemfile(\.lock)?|Dockerfile)$"
)
_TEST_CONFIG = re.compile(r"(^|/)(conftest\.py|pytest\.ini|tox\.ini|noxfile\.py|\.coveragerc|jest\.config\.[jt]s)$")
_LINE_RULES: tuple[tuple[FlagKind, re.Pattern[str]], ...] = (
    (
        "test_exclusion",
        re.compile(
            r"pytest\.mark\.(skip|xfail)|pytest\.skip\(|--deselect|\bxfail\b|\.skip\(|"
            r"\"(excluded?|exclusions|skip)\"|\bnot\s+required\b",
            re.I,
        ),
    ),
    (
        "permissions",
        re.compile(r"\bpermissions?\b|\"operations\"|\broles?\b|\bsudo\b|\bchmod\b|allow_all|\bgrant", re.I),
    ),
    (
        "network_access",
        re.compile(
            r"https?://|\brequests\.|\bhttpx\b|\burllib\b|\bsocket\b|\bcurl\b|"
            r"allowed_hosts|\begress\b|\bfetch\(",
            re.I,
        ),
    ),
)


# --- unified diff inspection -------------------------------------------------------------------------------


class DiffFile(Model):
    old_path: str | None
    new_path: str | None
    new_mode: str | None = None
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()

    @property
    def paths(self) -> list[str]:
        return [p for p in (self.old_path, self.new_path) if p is not None]


def _strip_prefix(raw: str) -> str | None:
    raw = raw.split("\t", 1)[0].strip()
    if raw == "/dev/null":
        return None
    if raw.startswith('"'):
        raise RepairError("unsafe_path", "Quoted (escaped) paths are not accepted in a patch")
    return raw[2:] if raw[:2] in ("a/", "b/") else raw


def parse_unified_diff(text: str) -> list[DiffFile]:
    """Files, modes and changed lines of a git-style unified diff. Data only; nothing is applied here."""
    files: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        if line.startswith("diff --git "):
            current = {"old_path": None, "new_path": None, "new_mode": None, "added": [], "removed": []}
            files.append(current)
            continue
        if current is None:
            if line.startswith("--- "):  # plain unified diff without a git header
                current = {"old_path": None, "new_path": None, "new_mode": None, "added": [], "removed": []}
                files.append(current)
            else:
                continue
        if line.startswith("--- ") and not current["added"] and not current["removed"]:
            current["old_path"] = _strip_prefix(line[4:])
        elif line.startswith("+++ ") and not current["added"] and not current["removed"]:
            current["new_path"] = _strip_prefix(line[4:])
        elif line.startswith(("new file mode ", "new mode ")):
            current["new_mode"] = line.rsplit(" ", 1)[1]
        elif line.startswith(("rename from ", "copy from ")):
            current["old_path"] = line.split(" ", 2)[2]
        elif line.startswith(("rename to ", "copy to ")):
            current["new_path"] = line.split(" ", 2)[2]
        elif line.startswith("GIT binary patch") or line.startswith("Binary files "):
            raise RepairError("binary_patch", "Binary patches are not accepted")
        elif line.startswith("+") and not line.startswith("+++"):
            current["added"].append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            current["removed"].append(line[1:])
    if not files:
        raise RepairError("empty_patch", "The patch changes no file")
    return [DiffFile(**{k: tuple(v) if isinstance(v, list) else v for k, v in f.items()}) for f in files]


def _clean_path(path: str) -> str:
    """Legacy repo-path rules (no absolute, '..', backslash, control characters) plus the held-out wall."""
    if layer_allowlist.is_heldout(path):
        raise RepairError("heldout_path", f"'{path}' is in the held-out set, which no repair may touch")
    try:
        clean = layer_allowlist._clean(path)  # noqa: SLF001 - reuse the exact legacy normaliser
    except ValueError as exc:
        raise RepairError("unsafe_path", f"'{path}' is not a valid repo path: {exc}") from None
    if any(part.casefold() == ".git" for part in clean.split("/")):
        raise RepairError("unsafe_path", f"'{path}' is inside a git folder")
    return clean


def _allowed(path: str, entries: Iterable[str]) -> bool:
    return any(path.startswith(e) if e.endswith("/") else path == e for e in entries)


class PolicyReport(Model):
    layer: Layer
    files: tuple[str, ...]
    lines_changed: int
    flags: tuple[RiskFlag, ...]

    @property
    def requires_review(self) -> bool:
        return bool(self.flags)


def check_patch_policy(patch: str, layer: Layer, policy: RepairPolicy) -> PolicyReport:
    """Reject paths outside the layer, held-out/hidden paths, links and oversize patches; flag risky edits."""
    rules = policy.for_layer(layer)
    files = parse_unified_diff(patch)
    paths: list[str] = []
    flags: list[RiskFlag] = []
    for entry in files:
        if entry.new_mode in _LINK_MODES:
            raise RepairError("link_in_patch", f"'{entry.new_path}' would become a link, which is not allowed")
        for raw in entry.paths:
            path = _clean_path(raw)
            if _allowed(path, policy.hidden_paths):
                raise RepairError("heldout_path", f"'{path}' holds hidden cases, which no repair may touch")
            if not _allowed(path, rules.allowed_paths):
                raise RepairError(
                    "outside_layer",
                    f"'{path}' is outside what a {layer} repair may change ({', '.join(rules.allowed_paths)})",
                )
            if path not in paths:
                paths.append(path)
        flags.extend(_flags(entry, policy))
    if len(paths) > rules.max_files:
        raise RepairError("too_many_files", f"The patch touches {len(paths)} files; the limit is {rules.max_files}")
    lines = sum(len(f.added) + len(f.removed) for f in files)
    if lines > rules.max_lines_changed:
        error = patch_size.PatchTooLarge(lines, rules.max_lines_changed)
        raise RepairError("patch_too_large", str(error)) from error
    unique = {(f.kind, f.path): f for f in flags}
    return PolicyReport(layer=layer, files=tuple(paths), lines_changed=lines, flags=tuple(unique.values()))


def _flags(entry: DiffFile, policy: RepairPolicy) -> list[RiskFlag]:
    found: list[RiskFlag] = []
    for path in entry.paths:
        if _DEPENDENCY_FILES.search(path):
            found.append(RiskFlag(kind="dependencies", path=path, evidence="dependency manifest changed"))
        if _TEST_CONFIG.search(path):
            found.append(RiskFlag(kind="test_exclusion", path=path, evidence="test configuration changed"))
        if path in policy.check_files or _allowed(path, policy.for_layer("evaluator").allowed_paths):
            found.append(RiskFlag(kind="evaluation_criteria", path=path, evidence="evaluation criteria changed"))
    path = entry.new_path or entry.old_path or ""
    for line in (*entry.added, *entry.removed):
        for kind, pattern in _LINE_RULES:
            if pattern.search(line):
                found.append(RiskFlag(kind=kind, path=path, evidence=line.strip()[:160]))
    return found


# --- criterion weakening (reuses susanoo_evaluator to canonicalise checks) --------------------------------


class CriteriaDiff(Model):
    before_digest: str
    after_digest: str
    removed: tuple[str, ...] = ()
    made_optional: tuple[str, ...] = ()
    severity_lowered: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    added: tuple[str, ...] = ()
    exclusions_added: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return self.before_digest != self.after_digest

    @property
    def weakened(self) -> bool:
        return bool(self.removed or self.made_optional or self.severity_lowered or self.exclusions_added)


_SEVERITY = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _index_checks(text: str | None, source: str) -> tuple[dict[str, dict[str, Any]], set[str]]:
    if text is None:
        return {}, set()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RepairError("criteria_unreadable", f"{source} is not valid JSON: {exc.msg}") from None
    groups: list[tuple[str, list[Any], bool]] = []
    exclusions: set[str] = set()
    if isinstance(data, dict) and isinstance(data.get("cases"), list):
        for case in data["cases"]:
            groups.append((str(case.get("id", "")), case.get("checks") or [], bool(case.get("required", True))))
            if case.get("visibility") == "excluded":
                exclusions.add(str(case.get("id")))
    elif isinstance(data, dict):
        groups.append(("", data.get("checks") or [], True))
    elif isinstance(data, list):
        groups.append(("", data, True))
    if isinstance(data, dict):
        for key in ("exclusions", "excluded_cases", "excluded_checks", "skip"):
            exclusions.update(str(x) for x in data.get(key) or [])
    checks: dict[str, dict[str, Any]] = {}
    for case_id, items, case_required in groups:
        for raw in items:
            try:
                check = parse_check(raw).model_dump(mode="json")
            except ValidationError as exc:
                raise RepairError("criteria_unreadable", f"{source}: invalid check: {exc.errors()[0]['msg']}") from None
            check["required"] = check["required"] and case_required
            checks[f"{case_id}:{check['id']}" if case_id else check["id"]] = check
    return checks, exclusions


def compare_criteria(before: dict[str, str | None], after: dict[str, str | None]) -> CriteriaDiff:
    """Compare check sets per check file; removal, optional, lower severity or new exclusions are weakening."""
    b_all: dict[str, dict[str, Any]] = {}
    a_all: dict[str, dict[str, Any]] = {}
    b_ex: set[str] = set()
    a_ex: set[str] = set()
    for path in sorted(set(before) | set(after)):
        checks, ex = _index_checks(before.get(path), path)
        b_all.update({f"{path}#{k}": v for k, v in checks.items()})
        b_ex |= {f"{path}#{x}" for x in ex}
        checks, ex = _index_checks(after.get(path), path)
        a_all.update({f"{path}#{k}": v for k, v in checks.items()})
        a_ex |= {f"{path}#{x}" for x in ex}
    removed = sorted(k for k in b_all if k not in a_all and b_all[k]["required"])
    optional = sorted(k for k in b_all if k in a_all and b_all[k]["required"] and not a_all[k]["required"])
    lowered = sorted(
        k for k in b_all if k in a_all and _SEVERITY[a_all[k]["severity"]] < _SEVERITY[b_all[k]["severity"]]
    )
    modified = sorted(k for k in b_all if k in a_all and content_hash(b_all[k]) != content_hash(a_all[k]))
    return CriteriaDiff(
        before_digest=content_hash({"checks": b_all, "exclusions": sorted(b_ex)}),
        after_digest=content_hash({"checks": a_all, "exclusions": sorted(a_ex)}),
        removed=tuple(removed),
        made_optional=tuple(optional),
        severity_lowered=tuple(lowered),
        modified=tuple(modified),
        added=tuple(sorted(k for k in a_all if k not in b_all)),
        exclusions_added=tuple(sorted(a_ex - b_ex)),
    )


# --- isolated worktree ------------------------------------------------------------------------------------


def _git_env(home: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "LANG": "C",
    }


class IsolatedWorktree:
    """A pinned clone of the customer repo (never the Susanoo repo) plus a detached worktree, in a temp dir.

    The clone comes from ``acquire_git`` (hooks/filters off, commit verified); the worktree is added with
    ``git worktree add --detach``. Everything is deleted on exit. [Spec §17; T32]
    """

    def __init__(self, source: GitSource, *, parent: Path | None = None):
        if not COMMIT.match(source.commit):
            raise RepairError("unpinned_source", "Repairs need a pinned 40-character commit")
        self.source = source
        self._parent = parent
        self.root: Path | None = None

    def __enter__(self) -> IsolatedWorktree:
        self.root = Path(tempfile.mkdtemp(prefix="susanoo-repair-", dir=self._parent))
        (self.root / "home").mkdir()
        try:
            acquire_git(self.source, self.root / "clone")
            self._git(
                ["worktree", "add", "--quiet", "--detach", str(self.root / "tree"), self.source.commit],
                cwd=self.root / "clone",
            )
        except SourceError as exc:
            self.__exit__(None, None, None)
            raise RepairError("source_unavailable", str(exc)) from exc
        if self.head() != self.source.commit:
            self.__exit__(None, None, None)
            raise RepairError("commit_mismatch", "The worktree is not at the pinned commit")
        return self

    def __exit__(self, *_: object) -> None:
        if self.root is not None:
            shutil.rmtree(self.root, ignore_errors=True)
            self.root = None

    @property
    def path(self) -> Path:
        if self.root is None:
            raise RepairError("closed", "The worktree is not open")
        return self.root / "tree"

    def _git(self, args: list[str], *, cwd: Path | None = None, stdin: str | None = None) -> str:
        assert self.root is not None
        result = subprocess.run(
            ["git", *GIT_CONFIG, "-c", "core.attributesFile=/dev/null", *args],
            cwd=cwd or self.path,
            env=_git_env(self.root / "home"),
            input=stdin,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode != 0:
            raise SourceError("git_failed", f"git {args[0]} failed: {result.stderr.strip()[-300:]}")
        return result.stdout

    def head(self) -> str:
        return self._git(["rev-parse", "HEAD"]).strip()

    def read_base(self, path: str) -> str | None:
        try:
            return self._git(["show", f"{self.source.commit}:{path}"])
        except SourceError:
            return None

    def read_current(self, path: str) -> str | None:
        target = self.path / path
        return target.read_text(encoding="utf-8") if target.is_file() and not target.is_symlink() else None

    def apply(self, patch: str) -> int:
        """Apply a unified diff to the index and tree; re-verify with git's own listing. Returns lines changed."""
        try:
            self._git(["apply", "--check", "--index", "--whitespace=nowarn", "-"], stdin=patch)
            self._git(["apply", "--index", "--whitespace=nowarn", "-"], stdin=patch)
            raw = self._git(["diff", "--cached", "--raw", "-z", "--no-renames", "--no-ext-diff", self.source.commit])
            # git's own count of the patch text (the clone marks every path -diff, so `diff --numstat` says binary)
            numstat = self._git(["apply", "--numstat", "-"], stdin=patch)
        except SourceError as exc:
            raise RepairError("patch_does_not_apply", str(exc)) from exc
        parts = raw.split("\0")
        for i in range(0, len(parts) - 1, 2):
            meta = parts[i].split()
            if len(meta) >= 2 and meta[1] in _LINK_MODES:
                raise RepairError("link_in_patch", f"'{parts[i + 1]}' became a link")
            _clean_path(parts[i + 1])
        return patch_size.count_from_numstat(numstat)

    def digest(self) -> str:
        return tree_digest(self.path)[0]

    def visible_files(self, allowed: Iterable[str], hidden: Iterable[str], max_bytes: int = 200_000) -> dict[str, str]:
        """Allowlisted, non-hidden text files for a patch generator; held-out content never leaves here."""
        allowed, hidden = tuple(allowed), tuple(hidden)
        out: dict[str, str] = {}
        total = 0
        for path in sorted(self.path.rglob("*")):
            rel = path.relative_to(self.path).as_posix()
            if rel.startswith(".git") or path.is_symlink() or not path.is_file():
                continue
            if layer_allowlist.is_heldout(rel) or _allowed(rel, hidden) or not _allowed(rel, allowed):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            total += len(text)
            if total > max_bytes:
                break
            out[rel] = text
        return out


# --- patch sources ----------------------------------------------------------------------------------------


class PatchContext(Model):
    """Everything a generator may see. Hidden/held-out cases are never part of it [Spec §17]."""

    finding_id: str
    layer: Layer
    summary: str
    failing_check_ids: tuple[str, ...]
    reduced_case: dict[str, Any]
    files: dict[str, str]
    allowed_paths: tuple[str, ...]


class GeneratedPatch(Model):
    diff: str = Field(min_length=1)
    rationale: str = Field(min_length=1, max_length=4000)


class PatchProvider(Protocol):
    name: str
    sends_source_off_host: bool  # True for a hosted model provider; False for a configured local provider

    async def propose(self, context: PatchContext) -> GeneratedPatch: ...


class SourceUploadApproval(Model):
    project_id: str
    approved_by: str = Field(min_length=1)
    provider: str


class PatchProposal(Model):
    finding_id: str
    layer: Layer
    origin: Literal["manual_upload", "generated"]
    diff: str = Field(min_length=1)
    rationale: str
    provider: str | None = None

    @property
    def patch_digest(self) -> str:
        return content_hash({"diff": self.diff, "layer": self.layer})


def manual_patch(finding_id: str, layer: Layer, diff: str, rationale: str) -> PatchProposal:
    parse_unified_diff(diff)
    return PatchProposal(
        finding_id=finding_id, layer=layer, origin="manual_upload", diff=diff, rationale=_scrub(rationale)
    )


def assert_no_hidden_cases(
    context: PatchContext, hidden_case_ids: Iterable[str], hidden_markers: Iterable[str] = ()
) -> None:
    """Fail closed if any hidden case id, held-out path or hidden content marker reaches the generator."""
    if context.reduced_case.get("visibility") == "hidden":
        raise RepairError("hidden_case_exposure", "The reduced case is hidden and cannot go to a generator")
    for path in context.files:
        if layer_allowlist.is_heldout(path):
            raise RepairError("hidden_case_exposure", f"'{path}' is held out and cannot go to a generator")
    blob = json.dumps(context.model_dump(mode="json"), ensure_ascii=False)
    for marker in (*hidden_case_ids, *hidden_markers):
        if marker and re.search(rf"(?<![A-Za-z0-9_]){re.escape(marker)}(?![A-Za-z0-9_])", blob):
            raise RepairError("hidden_case_exposure", "Hidden case material would reach the patch generator")


async def generate_patch(
    provider: PatchProvider,
    context: PatchContext,
    *,
    hidden_case_ids: Iterable[str],
    hidden_markers: Iterable[str] = (),
    upload_approval: SourceUploadApproval | None = None,
) -> PatchProposal:
    """Ask a provider for a patch. Off-host providers need explicit project approval; inputs are checked first."""
    if provider.sends_source_off_host and (upload_approval is None or upload_approval.provider != provider.name):
        raise RepairError(
            "source_upload_not_approved",
            "Sending source to a model provider needs project approval; use a local provider or upload a patch",
        )
    assert_no_hidden_cases(context, hidden_case_ids, hidden_markers)
    out = await provider.propose(context)
    parse_unified_diff(out.diff)
    return PatchProposal(
        finding_id=context.finding_id,
        layer=context.layer,
        origin="generated",
        diff=out.diff,
        rationale=_scrub(out.rationale),
        provider=provider.name,
    )


def _scrub(text: str) -> str:
    from susanoo.integrations.tracing import scrub_text  # lazy: heavy import, only for rationale text

    return scrub_text(text)


# --- validation gate --------------------------------------------------------------------------------------


class UnitTestCommand(Model):
    """Owner-approved argv; never a shell [Spec §17]."""

    argv: tuple[str, ...] = Field(min_length=1)
    timeout_seconds: int = Field(default=300, ge=1, le=3600)
    env: dict[str, str] = Field(default_factory=dict)
    cpu_seconds: int = Field(default=300, ge=1)

    @property
    def digest(self) -> str:
        return content_hash(self.model_dump(mode="json"))


def run_unit_tests(
    command: UnitTestCommand, tree: Path, *, approved_digest: str, cancelled: threading.Event | None = None
) -> tuple[bool, str]:
    if approved_digest != command.digest:
        raise RepairError("command_not_approved", "The unit-test command differs from the approved digest")
    if resource is None:
        raise RepairError("unsupported_host", "Repair execution requires the approved Linux resource-limit boundary")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "HOME": str(tree), **command.env}

    def limits() -> None:
        resource.setrlimit(resource.RLIMIT_CPU, (command.cpu_seconds, command.cpu_seconds))
        resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))

    try:
        process = subprocess.Popen(
            list(command.argv),
            cwd=tree,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            preexec_fn=limits,
            start_new_session=True,
        )
    except FileNotFoundError:
        return False, f"{command.argv[0]} not found"
    output, overflow = bytearray(), threading.Event()

    def stop():
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def drain():
        while chunk := process.stdout.read(4096):
            room = max(0, 65536 - len(output))
            output.extend(chunk[:room])
            if len(chunk) > room:
                overflow.set()
                stop()

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    deadline, reason = time.monotonic() + command.timeout_seconds, ""
    try:
        while process.poll() is None:
            if cancelled and cancelled.is_set():
                reason = "unit tests cancelled"
                break
            if overflow.is_set():
                reason = "unit test output exceeded 64 KiB"
                break
            if time.monotonic() >= deadline:
                reason = "unit tests timed out"
                break
            time.sleep(0.025)
    finally:
        stop()
        process.wait()
        reader.join(timeout=2)
        process.stdout.close()
    if overflow.is_set():
        reason = "unit test output exceeded 64 KiB"
    return not reason and process.returncode == 0, reason or _scrub(output.decode("utf-8", "replace")[-1500:])


async def _unit_tests_async(command, tree, digest, cancellation):
    stopped = threading.Event()
    task = asyncio.create_task(
        asyncio.to_thread(run_unit_tests, command, tree, approved_digest=digest, cancelled=stopped)
    )
    try:
        while not task.done():
            if cancellation and cancellation():
                stopped.set()
            try:
                await asyncio.wait_for(asyncio.shield(task), 0.1)
            except TimeoutError:
                pass
        return await task
    except asyncio.CancelledError:
        stopped.set()
        try:
            await asyncio.shield(task)
        finally:
            raise


class RepairHarness(Protocol):
    """Runs public material against a tree (reduced reproduction and valid controls)."""

    async def reproduce(self, tree: Path) -> Verdict: ...

    async def controls(self, tree: Path) -> dict[str, Verdict]: ...


class HeldoutSummary(Model):
    """The only thing the separate held-out evaluator may return: counts and the split digest."""

    split_digest: str
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    unknown: int = Field(ge=0)


class HeldoutEvaluator(Protocol):
    split_digest: str

    async def evaluate(self, tree: Path) -> HeldoutSummary | dict[str, Any]: ...


class HeldoutExposureLedger:
    """Counts how often a held-out split gave feedback; at the threshold the split must rotate [Spec §17]."""

    def __init__(self, threshold: int = 5, path: Path | None = None):
        self.threshold = threshold
        self.path = path
        self._counts: dict[str, int] = json.loads(path.read_text()) if path and path.exists() else {}

    def count(self, split: str) -> int:
        return self._counts.get(split, 0)

    def rotation_required(self, split: str) -> bool:
        return self.count(split) >= self.threshold

    def record(self, split: str) -> int:
        self._counts[split] = self.count(split) + 1
        if self.path:
            self.path.write_text(json.dumps(self._counts, sort_keys=True))
        return self._counts[split]


class GateCheck(Model):
    name: str
    passed: bool
    detail: str


class LayerPins(Model):
    agent_commit: str
    agent_after: str
    simulator_before: str
    simulator_after: str
    evaluator_before: str
    evaluator_after: str
    criteria_before: str
    criteria_after: str


class RepairValidation(Model):
    finding_id: str
    layer: Layer
    origin: str
    base_commit: str
    patch_digest: str
    outcome: Literal["accepted", "rejected", "inconclusive"]
    checks: tuple[GateCheck, ...]
    flags: tuple[RiskFlag, ...]
    pins: LayerPins
    heldout: HeldoutSummary | None
    heldout_exposures: int

    @property
    def evidence_digest(self) -> str:
        return content_hash(self.model_dump(mode="json"))


async def validate_repair(
    source: GitSource,
    proposal: PatchProposal,
    *,
    harness: RepairHarness,
    heldout: HeldoutEvaluator,
    ledger: HeldoutExposureLedger,
    unit_tests: UnitTestCommand,
    unit_tests_approved_digest: str,
    simulator_digest: str,
    evaluator_pin: str | None = None,
    failing_check_ids: Iterable[str] = (),
    policy: RepairPolicy | None = None,
    parent: Path | None = None,
    cancellation: Callable[[], bool] | None = None,
) -> RepairValidation:
    """F06 gate for a code patch on one layer. Policy refusals raise; gate failures return ``rejected``."""
    policy = policy or RepairPolicy()
    report = check_patch_policy(proposal.diff, proposal.layer, policy)
    evaluator_before = evaluator_pin or evaluator_digest()
    checks: list[GateCheck] = []

    def result(outcome: str, after_tree: str, crit: CriteriaDiff | None, summary=None, exposures=0):
        changed = {proposal.layer: after_tree}
        crit_b = crit.before_digest if crit else ""
        return RepairValidation(
            finding_id=proposal.finding_id,
            layer=proposal.layer,
            origin=proposal.origin,
            base_commit=source.commit,
            patch_digest=proposal.patch_digest,
            outcome=outcome,  # type: ignore[arg-type]
            checks=tuple(checks),
            flags=report.flags,
            pins=LayerPins(
                agent_commit=source.commit,
                agent_after=changed.get("agent", source.commit),
                simulator_before=simulator_digest,
                simulator_after=changed.get("simulator", simulator_digest),
                evaluator_before=evaluator_before,
                evaluator_after=changed.get("evaluator", evaluator_before),
                criteria_before=crit_b,
                criteria_after=crit.after_digest if crit else crit_b,
            ),
            heldout=summary,
            heldout_exposures=exposures,
        )

    with IsolatedWorktree(source, parent=parent) as base:
        verdict = await harness.reproduce(base.path)
        if verdict != "FAIL":
            checks.append(
                GateCheck(
                    name="reproduction_before_repair",
                    passed=False,
                    detail=f"the reduced case is {verdict} before the repair (not reproduced)",
                )
            )
            return result("inconclusive", "", None)
        checks.append(GateCheck(name="reproduction_before_repair", passed=True, detail="reduced case fails"))
        controls_before = await harness.controls(base.path)
        invalid = sorted(k for k, v in controls_before.items() if v != "PASS")
        if invalid:
            raise RepairError("invalid_controls", f"Controls {invalid} do not pass before the repair")

    with IsolatedWorktree(source, parent=parent) as tree:
        before_tree = tree.digest()
        lines = tree.apply(proposal.diff)
        if lines > policy.for_layer(proposal.layer).max_lines_changed:
            cap = policy.for_layer(proposal.layer).max_lines_changed
            raise RepairError("patch_too_large", str(patch_size.PatchTooLarge(lines, cap)))
        after_tree = content_hash({"base": source.commit, "tree": tree.digest(), "was": before_tree})
        criteria = compare_criteria(
            {p: tree.read_base(p) for p in policy.check_files}, {p: tree.read_current(p) for p in policy.check_files}
        )
        if criteria.weakened:
            raise RepairError(
                "criteria_weakened",
                "The patch removes or weakens required checks: "
                + ", ".join(
                    criteria.removed + criteria.made_optional + criteria.severity_lowered + criteria.exclusions_added
                ),
            )
        failing = set(failing_check_ids)
        rewritten = [k for k in criteria.modified if k.split("#", 1)[1].rsplit(":", 1)[-1] in failing]
        if proposal.layer != "evaluator" and criteria.changed:
            raise RepairError("criteria_changed", f"A {proposal.layer} repair cannot change evaluation criteria")
        if rewritten:
            raise RepairError("original_failure_rewritten", f"The patch rewrites the failing check(s) {rewritten}")
        checks.append(
            GateCheck(
                name="criteria_not_weakened",
                passed=True,
                detail="criteria unchanged" if not criteria.changed else "criteria changed; review",
            )
        )

        ok, log = await _unit_tests_async(unit_tests, tree.path, unit_tests_approved_digest, cancellation)
        checks.append(GateCheck(name="unit_tests", passed=ok, detail=log[-300:] or ("passed" if ok else "failed")))

        after = await harness.reproduce(tree.path)
        checks.append(
            GateCheck(
                name="reduced_reproduction_fixed",
                passed=after == "PASS",
                detail=f"reduced case is {after} after the repair",
            )
        )
        controls_after = await harness.controls(tree.path)
        broken = sorted(k for k in controls_before if controls_after.get(k) != "PASS")
        checks.append(
            GateCheck(
                name="valid_controls_preserved",
                passed=not broken,
                detail=f"broken controls: {broken}" if broken else f"{len(controls_before)} still pass",
            )
        )

        split = heldout.split_digest
        if ledger.rotation_required(split):
            checks.append(
                GateCheck(
                    name="heldout_matches",
                    passed=False,
                    detail="held-out split exposure limit reached; rotate cases before retesting",
                )
            )
            return result("inconclusive", after_tree, criteria, None, ledger.count(split))
        raw = await heldout.evaluate(tree.path)
        exposures = ledger.record(split)
        try:
            summary = HeldoutSummary.model_validate(raw.model_dump() if isinstance(raw, BaseModel) else raw)
        except ValidationError as exc:
            raise RepairError("heldout_overshared", "The held-out evaluator returned more than counts") from exc
        if summary.split_digest != split:
            raise RepairError("heldout_split_mismatch", "The held-out evaluator ran a different split")
        clean = summary.failed == 0 and summary.unknown == 0 and summary.passed > 0
        checks.append(
            GateCheck(
                name="heldout_matches",
                passed=clean,
                detail=f"{summary.passed} passed, {summary.failed} failed, {summary.unknown} unknown",
            )
        )
    outcome = "accepted" if all(c.passed for c in checks) else "rejected"
    return result(outcome, after_tree, criteria, summary, exposures)


# --- base movement and publication -------------------------------------------------------------------------


class BaseResolver(Protocol):
    async def current_base(self) -> str: ...


class PublicationApproval(Model):
    reviewer: str = Field(min_length=1)
    patch_digest: str
    evidence_digest: str


class PublicationRequest(Model):
    """What the platform needs to open a DRAFT PR at the pinned base. This module never publishes."""

    publication_key: str
    finding_id: str
    layer: Layer
    repository: str
    base_commit: str
    patch: str
    patch_digest: str
    rationale: str
    validation_evidence_digest: str
    flags: tuple[RiskFlag, ...]
    pins: LayerPins
    approved_by: str
    draft: Literal[True] = True
    auto_merge: Literal[False] = False


class StaleProposal(Model):
    status: Literal["stale"] = "stale"
    validated_base: str
    current_base: str
    patch_digest: str


def publication_key(repository: str, base_commit: str, proposal: PatchProposal) -> str:
    return content_hash(
        {"repo": repository, "base": base_commit, "patch": proposal.patch_digest, "finding": proposal.finding_id}
    )


async def prepare_publication(
    proposal: PatchProposal,
    validation: RepairValidation,
    *,
    repository: str,
    approval: PublicationApproval | None,
    base: BaseResolver,
    revalidate: Callable[[str], Awaitable[RepairValidation]] | None = None,
) -> PublicationRequest | StaleProposal:
    """Never returns a request for a claim validated on a commit other than the current base [Spec §17; T32]."""
    if validation.patch_digest != proposal.patch_digest:
        raise RepairError("validation_mismatch", "The validation is for a different patch")
    current = await base.current_base()
    if current != validation.base_commit:
        if revalidate is None:
            return StaleProposal(
                validated_base=validation.base_commit, current_base=current, patch_digest=proposal.patch_digest
            )
        validation = await revalidate(current)
        if validation.base_commit != current or validation.patch_digest != proposal.patch_digest:
            raise RepairError("revalidation_mismatch", "Revalidation did not run this patch on the current base")
        if await base.current_base() != current:
            return StaleProposal(
                validated_base=current, current_base=await base.current_base(), patch_digest=proposal.patch_digest
            )
    if validation.outcome != "accepted":
        raise RepairError("not_validated", f"The repair gate outcome is {validation.outcome}")
    if (
        approval is None
        or approval.patch_digest != proposal.patch_digest
        or approval.evidence_digest != validation.evidence_digest
    ):
        raise RepairError("publication_not_authorized", "A reviewer must approve this exact patch and evidence")
    return PublicationRequest(
        publication_key=publication_key(repository, validation.base_commit, proposal),
        finding_id=proposal.finding_id,
        layer=proposal.layer,
        repository=repository,
        base_commit=validation.base_commit,
        patch=proposal.diff,
        patch_digest=proposal.patch_digest,
        rationale=proposal.rationale,
        validation_evidence_digest=validation.evidence_digest,
        flags=validation.flags,
        pins=validation.pins,
        approved_by=approval.reviewer,
    )


class PublicationMarker(Model):
    publication_key: str
    pull_request_url: str


class PublicationMarkerLookup(Protocol):
    """Provider-side marker search (e.g. a hidden key in the draft PR body), injected by the platform."""

    async def find_marker(self, publication_key: str) -> PublicationMarker | None: ...


class ReconcileResult(Model):
    action: Literal["already_published", "safe_to_retry"]
    marker: PublicationMarker | None = None


async def reconcile_publication(request: PublicationRequest, lookup: PublicationMarkerLookup) -> ReconcileResult:
    """After a crash, check the provider marker BEFORE any retry so a draft PR is never created twice."""
    marker = await lookup.find_marker(request.publication_key)
    if marker is not None:
        if marker.publication_key != request.publication_key:
            raise RepairError("marker_mismatch", "The provider returned a marker for another publication")
        return ReconcileResult(action="already_published", marker=marker)
    return ReconcileResult(action="safe_to_retry")
