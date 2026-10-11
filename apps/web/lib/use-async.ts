"use client";

import { useCallback, useEffect, useState } from "react";

type AsyncState<T> = { status: "loading" } | { status: "error"; message: string } | { status: "ready"; data: T };

/** Load once per key, with a retry that re-runs the loader. Results from another key or attempt read as loading. */
export function useAsync<T>(load: () => Promise<T>, key: string): AsyncState<T> & { retry: () => void } {
  const [attempt, setAttempt] = useState(0);
  const current = `${key}:${attempt}`;
  const [settled, setSettled] = useState<{ key: string; state: AsyncState<T> } | null>(null);

  useEffect(() => {
    let cancelled = false;
    load().then(
      (data) => !cancelled && setSettled({ key: current, state: { status: "ready", data } }),
      (e) => !cancelled && setSettled({ key: current, state: { status: "error", message: e instanceof Error ? e.message : "Something went wrong." } }),
    );
    return () => {
      cancelled = true;
    };
    // The loader closes over the same inputs that make up `key`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current]);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);
  const state: AsyncState<T> = settled && settled.key === current ? settled.state : { status: "loading" };
  return { ...state, retry };
}
