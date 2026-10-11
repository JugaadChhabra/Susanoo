"""Unit tests for guards/layer_allowlist.py."""

import pytest

from susanoo.guards import layer_allowlist as la

ALLOWED_CASES = [
    ("code", "backend/app.py"),
    ("code", "backend/sub/deep/x.py"),
    ("code", "web/index.html"),
    ("code", "./backend//app.py"),
    ("tool", "agent/tools.py"),
    ("prompt_policy", "agent/prompt.md"),
    ("prompt_policy", "agent/policy.yaml"),
    ("test", "scenarios/train/s1.json"),
    ("test", "scenarios/train/sub/s2.json"),
]

DENIED_CASES = [
    ("code", "agent/tools.py"),
    ("code", "agent/prompt.md"),
    ("code", "scenarios/train/s1.json"),
    ("code", "backendx/app.py"),
    ("code", "backend"),
    ("code", "Backend/app.py"),
    ("tool", "agent/prompt.md"),
    ("tool", "agent/tools.py.bak"),
    ("tool", "agent/tools.py/x"),
    ("tool", "backend/app.py"),
    ("prompt_policy", "agent/tools.py"),
    ("prompt_policy", "agent/prompt.md.bak"),
    ("prompt_policy", "backend/app.py"),
    ("test", "backend/app.py"),
    ("test", "scenarios/other/s.json"),
    ("test", "scenarios/train"),
    ("test", "scenarios/trainer/s.json"),
    ("test", "README.md"),
]


@pytest.mark.parametrize(("layer", "path"), ALLOWED_CASES)
def test_allowed(layer, path):
    la.check_paths(layer, [path])


@pytest.mark.parametrize(("layer", "path"), DENIED_CASES)
def test_denied(layer, path):
    with pytest.raises(la.OutsideLayer) as exc:
        la.check_paths(layer, [path])
    assert exc.value.layer == layer
    assert path in str(exc.value)
    assert layer in str(exc.value)


def test_every_layer_has_an_allowed_and_a_denied_case():
    assert {layer for layer, _ in ALLOWED_CASES} == set(la.ALLOWED)
    assert {layer for layer, _ in DENIED_CASES} == set(la.ALLOWED)


def test_allowed_paths_match_the_prd_mapping():
    assert la.allowed_paths("code") == ["backend/", "web/"]
    assert la.allowed_paths("tool") == ["agent/tools.py"]
    assert la.allowed_paths("prompt_policy") == ["agent/prompt.md", "agent/policy.yaml"]
    assert la.allowed_paths("test") == ["scenarios/train/"]


def test_allowed_paths_returns_a_copy():
    la.allowed_paths("code").append("evil/")
    assert la.allowed_paths("code") == ["backend/", "web/"]


def test_allowed_paths_are_accepted_by_the_coding_worker():
    from susanoo.integrations.coding_worker import _normalise_allowed

    for layer in la.ALLOWED:
        assert _normalise_allowed(la.allowed_paths(layer))


def test_unknown_layer_is_a_value_error():
    with pytest.raises(ValueError, match="Unknown layer"):
        la.allowed_paths("nope")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Unknown layer"):
        la.check_paths("nope", ["backend/a.py"])  # type: ignore[arg-type]


def test_one_bad_path_fails_the_whole_set():
    with pytest.raises(la.OutsideLayer) as exc:
        la.check_paths("code", ["backend/a.py", "agent/tools.py", "web/b.js"])
    assert exc.value.path == "agent/tools.py"


def test_empty_path_list_is_fine():
    la.check_paths("code", [])


BAD_STRINGS = [
    "/backend/app.py",
    "/etc/passwd",
    "backend/../agent/tools.py",
    "../backend/app.py",
    "backend/..",
    "backend\\app.py",
    "backend/app.py\x00",
    "backend/a\nb.py",
    "backend/a\tb.py",
    "",
    "   ",
    ".",
    "./",
]


@pytest.mark.parametrize("path", BAD_STRINGS)
@pytest.mark.parametrize("layer", sorted(la.ALLOWED))
def test_malformed_paths_are_rejected_for_every_layer(layer, path):
    with pytest.raises(la.OutsideLayer):
        la.check_paths(layer, [path])


HELDOUT_PATHS = [
    "scenarios/heldout/h1.json",
    "scenarios/heldout",
    "scenarios/heldout/",
    "Scenarios/HeldOut/h1.json",
    "SCENARIOS/HELDOUT/x",
    "scenarios/HELDOUT/sub/deep/x.json",
    "scenarios//heldout/x",
    "./scenarios/./heldout/x",
    "scenarios\\heldout\\x",
    "scenarios/heldout./x",
    "scenarios/heldout /x",
    "backend/scenarios/heldout/x",
    "scenarios/train/../heldout/x",
    "backend/../scenarios/heldout/x",
    "/repo/scenarios/heldout/x",
]


@pytest.mark.parametrize("path", HELDOUT_PATHS)
def test_is_heldout(path):
    assert la.is_heldout(path)


@pytest.mark.parametrize("path", HELDOUT_PATHS)
@pytest.mark.parametrize("layer", sorted(la.ALLOWED))
def test_heldout_is_denied_for_every_layer(layer, path):
    with pytest.raises(la.OutsideLayer, match="held-out"):
        la.check_paths(layer, [path])


@pytest.mark.parametrize(
    "path",
    ["scenarios/train/a.json", "scenarios/heldout2/a.json", "heldout/a.json", "web/heldout/a.js"],
)
def test_near_misses_are_not_heldout(path):
    assert not la.is_heldout(path)


def test_heldout_denied_even_if_the_layer_would_otherwise_allow_it():
    # A folder allowed for `code` that happens to contain scenarios/heldout is still off limits.
    with pytest.raises(la.OutsideLayer, match="held-out"):
        la.check_paths("code", ["backend/scenarios/heldout/x.py"])


@pytest.mark.parametrize("head", ["diagnostician", "fixer", "Fixer", " DIAGNOSTICIAN "])
@pytest.mark.parametrize("path", HELDOUT_PATHS)
def test_blind_heads_cannot_read_heldout(head, path):
    assert not la.is_readable_by(head, path)


@pytest.mark.parametrize("head", ["diagnostician", "fixer"])
def test_blind_heads_can_read_normal_files(head):
    for path in ["scenarios/train/a.json", "backend/app.py", "agent/prompt.md"]:
        assert la.is_readable_by(head, path)


@pytest.mark.parametrize("head", ["diagnostician", "fixer"])
@pytest.mark.parametrize("path", ["/abs/path", "../x", "a\\b", "a\x00b", ""])
def test_blind_heads_cannot_read_unclean_paths(head, path):
    assert not la.is_readable_by(head, path)


@pytest.mark.parametrize("head", ["verifier", "runner", "planner", "builder", "simulator", "agent_smith"])
def test_other_heads_are_not_restricted_here(head):
    assert la.is_readable_by(head, "scenarios/heldout/h1.json")


@pytest.mark.parametrize("path", ["backend/.git/config", "web/sub/.GIT/hooks/pre-commit"])
def test_nested_git_folders_are_refused(path):
    with pytest.raises(la.OutsideLayer, match="git folder"):
        la.check_paths("code", [path])
