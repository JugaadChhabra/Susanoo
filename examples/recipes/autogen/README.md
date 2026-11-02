# AutoGen recipe — untested, not claimed

AutoGen is not installed in this repository and no AutoGen code has been run against Susanoo. Nothing
here is a working or supported integration.

Intended approach (unverified): run the AutoGen team/agent inside `SusanooAgent.respond` and serve it
with `susanoo_sdk.serve` (JSONL), registering tools that call the approved `ToolClient` route from the
invocation context; or put it behind an HTTP wrapper and use `HttpAgentAdapter`.

Before this can be claimed: pin an AutoGen version, add a runnable deterministic example, and pass it
through `run_suite` in `tests/runtime/test_recipes.py`.
