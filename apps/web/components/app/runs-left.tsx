"use client";

import Link from "next/link";
import { RotateCw } from "lucide-react";
import { getRunQuota } from "@/lib/api";
import { runsLeftLabel } from "@/lib/projects";
import { useAsync } from "@/lib/use-async";
import { cn } from "@/lib/utils";

/** Runs the signed-in user can still start today. [PRD › Frontend and API › Screens › Projects] */
export function RunsLeft() {
  const state = useAsync(getRunQuota, "run-quota");

  if (state.status === "loading") {
    return <div className="h-9 w-48 animate-pulse rounded-lg bg-muted" aria-busy="true" aria-label="Loading runs left today" />;
  }

  if (state.status === "error") {
    return (
      <p role="alert" className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
        We couldn&apos;t check how many runs you have left today.
        <button
          type="button"
          onClick={state.retry}
          className="inline-flex min-h-9 items-center gap-1 rounded-lg px-2 underline underline-offset-2 hover:bg-muted focus-visible:outline-2 focus-visible:outline-ring"
        >
          <RotateCw className="size-3.5" aria-hidden /> Try again
        </button>
      </p>
    );
  }

  const blocked = state.data.paused || state.data.runs_left_today === 0;
  return (
    <p
      role="status"
      className={cn(
        "inline-flex flex-wrap items-center gap-x-2 rounded-lg px-3 py-2 text-sm",
        blocked ? "bg-amber-500/15 text-amber-800 dark:text-amber-200" : "bg-muted text-foreground",
      )}
    >
      {runsLeftLabel(state.data)}
      {blocked && (
        <Link href="/demo" className="font-medium underline underline-offset-2 focus-visible:outline-2 focus-visible:outline-ring">
          Watch the demo
        </Link>
      )}
    </p>
  );
}
