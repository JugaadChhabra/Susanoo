"use client";

import { useCallback, useEffect, useState } from "react";
import { fetchIntake } from "@/lib/intake-api";
import type { IntakeOut } from "@/types/generated";

const POLL_MS = 2500;

/**
 * What a live run is paused on (GET /runs/{id}/intake), loaded while the events say it is paused.
 * The pause event can arrive a moment before the run row records the pause, so a "nothing waiting"
 * answer is retried for as long as the events still say paused. `pauseKey` changes with each new pause.
 */
export function useRunPause(runId: string | null, pauseKey: string, paused: boolean) {
  const [loaded, setLoaded] = useState<{ key: string; intake: IntakeOut } | null>(null);
  const [failedKey, setFailedKey] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const key = `${runId}:${pauseKey}:${reload}`;

  useEffect(() => {
    if (!runId || !paused) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      try {
        const intake = await fetchIntake(runId);
        if (cancelled) return;
        setLoaded({ key, intake });
        setFailedKey(null);
        if (!intake.waiting) timer = setTimeout(load, POLL_MS);
      } catch {
        if (cancelled) return;
        setFailedKey(key); // keep trying, but let the screen say we're having trouble
        timer = setTimeout(load, POLL_MS);
      }
    };
    void load();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [runId, paused, key]);

  const retry = useCallback(() => setReload((n) => n + 1), []);
  const intake = paused && loaded?.key === key && loaded.intake.waiting ? loaded.intake : null;
  return { intake, failing: paused && failedKey === key, reload: retry };
}
