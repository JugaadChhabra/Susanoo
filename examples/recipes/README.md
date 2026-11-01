# Framework and hosted-platform recipes (I14)

Recipes connect agents built with a framework or hosted on a platform through the generic adapters
(I03 HTTP, I06/I07 JSONL SDKs, I08 CLI/container). Nothing here is a native connector, and a recipe is
only claimed once it passes tests (Spec §4, §19).

| Recipe | Path | Transport | Status |
|---|---|---|---|
| LangGraph | `langgraph/` | JSONL (Python SDK) | **Tested** with langgraph 1.2.14 through `run_suite` (`tests/runtime/test_recipes.py`) |
| HTTP / n8n-style webhook | `http_webhook/` | I03 sync HTTP | **Tested against local fake servers**; no live n8n or hosted vendor verified |
| CrewAI | `crewai/` | JSONL (Python SDK) | **Untested — not claimed** (CrewAI not installed) |
| AutoGen | `autogen/` | JSONL (Python SDK) | **Untested — not claimed** (AutoGen not installed) |

Every recipe follows the same rules: the agent only receives the `InvocationContext` and the message,
calls tools through the approved `ToolClient` route, reports tool events instead of inventing them, and
is graded from independent simulator evidence, never from its reply text.
