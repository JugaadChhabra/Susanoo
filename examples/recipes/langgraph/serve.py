"""Serve the LangGraph recipe over JSONL: ``python examples/recipes/langgraph/serve.py``."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from graph_agent import LangGraphAgent  # noqa: E402
from susanoo_sdk import serve  # noqa: E402

if __name__ == "__main__":
    serve(LangGraphAgent())
