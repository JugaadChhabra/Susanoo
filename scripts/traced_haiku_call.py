"""One real traced Haiku call through llm.call("simulator", ...), to confirm tracing in neatlogs.

Human step: costs a fraction of a cent. Run once the keys are set (never commit them):

    NEATLOGS_API_KEY=... ANTHROPIC_API_KEY=... uv run python scripts/traced_haiku_call.py

Prints the model, tokens, cost, trace_id and (if NEATLOGS_TRACE_URL_TEMPLATE is set) the trace link.
Otherwise find the trace in the neatlogs dashboard by the trace_id or by run_id "smoke".
"""

from __future__ import annotations

import asyncio
import os
import sys

import neatlogs

from susanoo.integrations import llm, tracing

REQUIRED_KEYS = ("NEATLOGS_API_KEY", "ANTHROPIC_API_KEY")  # the Anthropic SDK reads ANTHROPIC_API_KEY itself


async def main() -> int:
    missing = [name for name in REQUIRED_KEYS if not os.environ.get(name)]
    if missing:
        print(f"Not running: set {' and '.join(missing)} in the environment first.", file=sys.stderr)
        return 1

    tracing.setup()  # before the first Anthropic client is built
    with tracing.span("simulator", run_id="smoke"):
        result = await llm.call(
            "simulator",
            [{"role": "user", "content": "Reply with the single word: ok"}],
            max_tokens=16,
        )
        ids = tracing.current_ids()

    neatlogs.flush()  # make sure the spans are sent before the process exits
    print(f"model:      {result.model}")
    print(f"reply:      {result.text.strip()!r}")
    print(f"tokens:     {result.input_tokens} in / {result.output_tokens} out")
    print(f"cost (usd): {result.cost_usd:.6f}")
    if ids is None:
        print("trace_id:   (none; no span was active)")
        return 1
    trace_id, span_id = ids
    print(f"trace_id:   {trace_id}")
    print(f"span_id:    {span_id}")
    print(f"trace_url:  {tracing.trace_url(trace_id) or '(set NEATLOGS_TRACE_URL_TEMPLATE to get a link)'}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
