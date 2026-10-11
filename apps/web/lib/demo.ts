import type { RunSource } from "./use-run-events";

/**
 * Which run the public demo page replays. NEXT_PUBLIC_DEMO_RUN_ID names the saved run;
 * without it, development replays the bundled fixture and production shows an empty state.
 */
export function demoSource(runId: string | undefined, isProduction: boolean): RunSource | null {
  const id = runId?.trim();
  if (id) return { kind: "demo", runId: id };
  return isProduction ? null : { kind: "fixture" };
}
