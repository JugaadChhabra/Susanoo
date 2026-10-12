"use client";

import { useCallback, useEffect, useState } from "react";
import type { RunEvent } from "@/types/generated";
import fixtureEvents from "@/lib/fixtures/run-events.json";
import { getRun, TERMINAL_RUN_STATUSES } from "@/lib/api";
import { fetchDemoEvents, mergeEvents, openRunStream, replayEvents, type StreamStatus } from "@/lib/sse";

/** Where a run view gets its events. Everything downstream only sees the resulting array. */
export type RunSource =
  | { kind: "live"; runId: string }
  | { kind: "demo"; runId: string; intervalMs?: number }
  | { kind: "fixture"; intervalMs?: number };

export interface RunEventsState {
  events: RunEvent[];
  status: StreamStatus;
  error: string | null;
  restart: () => void;
}

export function useRunEvents(source: RunSource): RunEventsState {
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [status, setStatus] = useState<StreamStatus>("connecting");
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  const sourceKey = source.kind === "fixture" ? "fixture" : `${source.kind}:${source.runId}`;
  const intervalMs = source.kind === "live" ? undefined : source.intervalMs;

  // Reset when the source changes or the user retries (adjusting state during render, not in an effect).
  const resetKey = `${sourceKey}#${attempt}`;
  const [seenKey, setSeenKey] = useState(resetKey);
  if (seenKey !== resetKey) {
    setSeenKey(resetKey);
    setEvents([]);
    setError(null);
    setStatus("connecting");
  }

  useEffect(() => {
    let cancelled = false;
    let stop: (() => void) | null = null;
    const push = (event: RunEvent) => setEvents((cur) => mergeEvents(cur, [event]));

    const replay = (all: RunEvent[]) => {
      if (cancelled) return;
      setStatus("live");
      stop = replayEvents(all, { onEvent: push, onDone: () => setStatus("closed"), intervalMs }).close;
    };

    if (sourceKey === "fixture") {
      replay(fixtureEvents as RunEvent[]);
    } else if (sourceKey.startsWith("demo:")) {
      fetchDemoEvents(sourceKey.slice(5)).then(replay, (e: unknown) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "We couldn't load this demo run.");
        setStatus("closed");
      });
    } else {
      const runId = sourceKey.slice(5);
      stop = openRunStream(runId, {
        onEvent: push,
        onStatus: (s) => !cancelled && setStatus(s),
        onGiveUp: (message) => !cancelled && setError(message),
        isRunOver: async () => TERMINAL_RUN_STATUSES.has((await getRun(runId)).status),
      }).close;
    }
    return () => {
      cancelled = true;
      stop?.();
    };
  }, [sourceKey, intervalMs, attempt]);

  const restart = useCallback(() => setAttempt((n) => n + 1), []);
  return { events, status, error, restart };
}
