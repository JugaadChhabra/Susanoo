"""Focused worker tests for locally approved source/build producers."""

import os
import sys
from types import SimpleNamespace

import pytest
from susanoo_connectors.source import BuildRecipe, import_directory
from susanoo_contracts import SourceSpec

from susanoo_runner.platform_source_jobs import _bounded_build, _source


def test_source_inputs_are_bound_to_approved_recipe_and_commit():
    recipe = BuildRecipe(runtime="python", steps=[[sys.executable, "agent.py"]])
    data = SourceSpec(repository="acme/agent", commit="a" * 40, recipe_hash=recipe.digest, lockfile_hash="b" * 64)
    claim = SimpleNamespace(
        execution_manifest=SimpleNamespace(versions=[SimpleNamespace(kind="source", approved=True, data=data)])
    )
    _, source, actual, _ = _source(
        claim, {"source_url": "https://github.com/acme/agent.git", "recipe": recipe.model_dump(mode="json")}
    )
    assert source.commit == "a" * 40 and actual.digest == recipe.digest
    with pytest.raises(Exception, match="recipe differs from the approved"):
        _source(
            claim,
            {
                "source_url": "https://github.com/acme/agent.git",
                "recipe": {**recipe.model_dump(mode="json"), "timeout_seconds": 1200},
            },
        )


@pytest.mark.skipif(os.name != "posix", reason="process-group teardown is POSIX-specific")
def test_bounded_build_caps_log_and_kills_descendants(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "run.py").write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print('started', flush=True)\n"
        "time.sleep(2)\n"
    )
    snapshot = import_directory(source, tmp_path / "snapshot")
    recipe = BuildRecipe(runtime="python", steps=[[sys.executable, "run.py"]], timeout_seconds=1)
    result = _bounded_build(snapshot, recipe, recipe.digest, {}, 2048)
    assert result.status == "timed_out"
    assert len(result.log_tail.encode()) <= 2048


def test_unapproved_recipe_refused(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "agent.py").write_text("print('ok')\n")
    snapshot = import_directory(source, tmp_path / "snapshot")
    recipe = BuildRecipe(runtime="python", steps=[[sys.executable, "agent.py"]])
    with pytest.raises(Exception, match="not locally approved"):
        _bounded_build(snapshot, recipe, "0" * 64, {}, 1024)
