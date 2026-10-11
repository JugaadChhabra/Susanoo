"use client";

import { useEffect, useState } from "react";

import { type Health, getHealth } from "@/lib/api";

type State =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "loaded"; health: Health };

export function HealthCheck() {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [showDetails, setShowDetails] = useState(false);

  useEffect(() => {
    getHealth()
      .then((health) => setState({ kind: "loaded", health }))
      .catch((err: Error) => setState({ kind: "error", message: err.message }));
  }, []);

  if (state.kind === "loading") {
    return <p role="status">Checking that everything is running…</p>;
  }
  if (state.kind === "error") {
    return (
      <p role="alert" className="text-red-600">
        Susanoo isn&apos;t responding right now. {state.message}
      </p>
    );
  }

  const { health } = state;
  return (
    <div>
      <p role="status" className={health.status === "ok" ? "text-green-600" : "text-amber-600"}>
        {health.status === "ok"
          ? "Everything is running."
          : `Running with a problem: ${health.supabase.message}`}
      </p>
      <button
        type="button"
        className="mt-2 text-sm underline"
        onClick={() => setShowDetails((open) => !open)}
      >
        {showDetails ? "Hide details" : "Show details"}
      </button>
      {showDetails && (
        <pre className="mt-2 overflow-x-auto rounded bg-muted p-3 text-xs">
          {JSON.stringify(health, null, 2)}
        </pre>
      )}
    </div>
  );
}
