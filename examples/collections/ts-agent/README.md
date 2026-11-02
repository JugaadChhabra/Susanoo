# TypeScript collections demo agent (I07)

A port of `examples/collections/collections_demo/agent.py` onto `@susanoo/sdk`
(`packages/sdk-typescript`). Same parsing rules and the same nine modes, so the same 20-case suite
grades it from simulator evidence.

Runs on Node 22.18+ (native type stripping, no build step, no `npm install`):

```bash
node examples/collections/ts-agent/serve.ts --mode correct   # speaks JSONL protocol 1.0 on stdin/stdout
uv run python -m susanoo_runner.cli local --suite examples/collections/suite.json \
  --agent-cmd node examples/collections/ts-agent/serve.ts --mode false_completion
```

Tested end to end in `tests/runtime/test_typescript_sdk.py` (`correct` and `false_completion`).
The other modes are ported but only `correct`/`false_completion` are exercised by tests.
