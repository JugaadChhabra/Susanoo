# CrewAI recipe — untested, not claimed

CrewAI is not installed in this repository and no CrewAI code has been run against Susanoo. Nothing
here is a working or supported integration.

Intended approach (unverified): wrap a crew's kickoff in a `SusanooAgent.respond` and serve it with
`susanoo_sdk.serve` (JSONL), or expose it behind an HTTP wrapper and use `HttpAgentAdapter`. Crew tools
would call the environment only through the approved `ToolClient` route from the invocation context.

Before this can be claimed: pin a CrewAI version, add a runnable deterministic example, and pass it
through `run_suite` in `tests/runtime/test_recipes.py`.
