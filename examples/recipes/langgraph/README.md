# LangGraph recipe (tested)

`graph_agent.py` builds a small deterministic `StateGraph` (no model key) for the synthetic collections
domain and wraps it as a `SusanooAgent`; `serve.py` serves it over the JSONL bridge.

- Tested version: **langgraph 1.2.14** (Python 3.12).
- One LangGraph thread per Susanoo session (`InMemorySaver`, `thread_id = session_id`) gives multi-turn memory.
- The approved `ToolClient` goes through LangGraph's runtime `context` (`context_schema=Deps`), not graph
  state or `configurable`, so the route token is never checkpointed.

```bash
export PYTHONPATH=packages/evaluator:packages/simulator:packages/sdk-python:packages/runner:examples/collections
uv run python -m susanoo_runner.cli local --suite examples/collections/suite.json \
  --agent-cmd python examples/recipes/langgraph/serve.py
```

To adapt: keep `LangGraphAgent.respond` and replace the nodes. The graph must take `text` in and put
`reply` out. If your graph needs a model key, pass it as an approved secret reference, never in state.
