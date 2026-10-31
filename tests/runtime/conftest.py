"""Put Muaz-lane packages on the path until they join the uv workspace (docs/parallel/muaz/DEPENDENCIES.md)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for relative in (
    "packages/evaluator",
    "packages/simulator",
    "packages/sdk-python",
    "packages/runner",
    "packages/connectors",
    "examples/collections",
):
    path = str(ROOT / relative)
    if path not in sys.path:
        sys.path.insert(0, path)
