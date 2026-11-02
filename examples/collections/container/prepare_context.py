"""Assemble the Docker build context for the collections agent image (I08) [Spec §9].

Copies only the pure-Python packages the agent needs (contracts, evaluator, SDK) and the agent itself,
with a minimal ``collections_demo/__init__.py`` so the image does not pull in the simulator or FastAPI.
``python examples/collections/container/prepare_context.py <out_dir>`` then
``docker build -t susanoo-collections-agent <out_dir>``.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
PACKAGES = {
    "susanoo_contracts": ROOT / "packages/contracts/susanoo_contracts",
    "susanoo_evaluator": ROOT / "packages/evaluator/susanoo_evaluator",
    "susanoo_sdk": ROOT / "packages/sdk-python/susanoo_sdk",
}
AGENT_FILES = ("agent.py", "serve.py")
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


def prepare(out: Path) -> Path:
    if out.exists():
        shutil.rmtree(out)
    app = out / "app"
    app.mkdir(parents=True)
    for name, source in PACKAGES.items():
        shutil.copytree(source, app / name, ignore=IGNORE)
    demo = app / "collections_demo"
    demo.mkdir()
    for name in AGENT_FILES:
        shutil.copy2(ROOT / "examples/collections/collections_demo" / name, demo / name)
    (demo / "__init__.py").write_text('"""Collections agent only (container image)."""\n')
    shutil.copy2(HERE / "Dockerfile", out / "Dockerfile")
    return out


if __name__ == "__main__":
    print(prepare(Path(sys.argv[1]).resolve()))
