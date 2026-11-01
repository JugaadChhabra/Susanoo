// Serve the TypeScript demo agent over JSONL: ``node examples/collections/ts-agent/serve.ts --mode correct``.

import { parseArgs } from "node:util";
import { serve } from "../../../packages/sdk-typescript/src/index.ts";
import { CollectionsAgent, MODES, type Mode } from "./agent.ts";

const { values } = parseArgs({
  options: { mode: { type: "string", default: "correct" }, "hang-seconds": { type: "string", default: "3600" } },
  strict: true,
});
const mode = values.mode as Mode;
if (!(MODES as readonly string[]).includes(mode)) {
  process.stderr.write(`unknown mode ${mode}; expected one of ${MODES.join(", ")}\n`);
  process.exit(2);
}
await serve(new CollectionsAgent(mode, Number(values["hang-seconds"])));
