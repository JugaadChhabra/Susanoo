"use client";

import { useState } from "react";
import { Play } from "lucide-react";
import { demoSource } from "@/lib/demo";
import { RunView } from "./run-view";

/**
 * The saved demo run, replayed (screen 7). Used by /demo and embedded on the landing page;
 * without `autoplay` it waits for a click, so the replay starts when someone is watching.
 */
export function DemoReplay({ autoplay = false }: { autoplay?: boolean }) {
  const [playing, setPlaying] = useState(autoplay);
  const source = demoSource(process.env.NEXT_PUBLIC_DEMO_RUN_ID, process.env.NODE_ENV === "production");

  if (!source) {
    return (
      <div role="status" className="rounded-2xl bg-slate-900 p-6 text-sm text-slate-300 ring-1 ring-slate-800">
        The demo run isn&apos;t ready yet. Please check back soon.
      </div>
    );
  }
  if (!playing) {
    return (
      <div className="flex min-h-64 flex-col items-center justify-center gap-4 rounded-2xl bg-slate-950 p-8 text-center text-slate-300 ring-1 ring-slate-800">
        <p className="max-w-sm text-sm">Plays a saved run, step by step, in under a minute.</p>
        <button
          type="button"
          onClick={() => setPlaying(true)}
          className="inline-flex h-12 items-center gap-2 rounded-full bg-white px-6 font-medium text-slate-950 hover:bg-slate-200 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white"
        >
          <Play className="size-4" aria-hidden /> Play the replay
        </button>
      </div>
    );
  }
  return <RunView source={source} />;
}
