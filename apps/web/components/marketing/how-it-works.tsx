"use client";

import { useEffect, useState, useSyncExternalStore } from "react";
import { Pause, Play } from "lucide-react";
import { cn } from "@/lib/utils";
import { HEAD_STEPS, nextHeadIndex } from "@/lib/marketing";

const STEP_MS = 1800;

const REDUCED_QUERY = "(prefers-reduced-motion: reduce)";

function subscribeReducedMotion(onChange: () => void): () => void {
  const query = window.matchMedia(REDUCED_QUERY);
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}

function useReducedMotion(): boolean {
  return useSyncExternalStore(
    subscribeReducedMotion,
    () => window.matchMedia(REDUCED_QUERY).matches,
    () => false,
  );
}

/** The eight heads, lit one after another. Row two is the improvement loop. */
export function HowItWorks() {
  const reduced = useReducedMotion();
  const [active, setActive] = useState(0);
  const [paused, setPaused] = useState(false);
  const playing = !paused && !reduced;

  useEffect(() => {
    if (!playing) return;
    const timer = window.setInterval(() => setActive(nextHeadIndex), STEP_MS);
    return () => window.clearInterval(timer);
  }, [playing]);

  const rows = [HEAD_STEPS.slice(0, 4), HEAD_STEPS.slice(4)];

  return (
    <section id="how-it-works" aria-labelledby="how-heading" className="border-t border-[var(--mk-rule)] py-20 sm:py-28">
      <div className="mx-auto w-full max-w-6xl px-4 sm:px-6">
        <div className="flex flex-col gap-6 sm:flex-row sm:items-end sm:justify-between">
          <div className="max-w-2xl">
            <h2 id="how-heading" className="mk-display text-3xl leading-tight sm:text-5xl">
              Eight heads, one job each
            </h2>
            <p className="mt-4 text-lg leading-relaxed text-[var(--mk-storm)]">
              In the old story, Susanoo defeated Orochi, a serpent with eight heads. Here the eight heads work for you. They
              share one memory of the run and hand off to each other in order.
            </p>
          </div>
          {!reduced && (
            <button
              type="button"
              onClick={() => setPaused((p) => !p)}
              className="inline-flex h-10 w-fit items-center gap-2 rounded-full border border-[var(--mk-rule)] bg-white px-4 text-sm font-medium text-[var(--mk-ink)] transition-colors hover:border-[var(--mk-storm)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--mk-tide)]"
            >
              {paused ? <Play className="size-4" aria-hidden /> : <Pause className="size-4" aria-hidden />}
              {paused ? "Play the sequence" : "Pause the sequence"}
            </button>
          )}
        </div>

        <div className="mt-12 space-y-10">
          {rows.map((row, r) => (
            <div key={r}>
              <p className="mb-3 text-sm font-medium text-[var(--mk-storm)]">
                {r === 0
                  ? "First, it builds"
                  : "Then it loops: up to five rounds, stopping early when two rounds in a row don't improve"}
              </p>
              <ol
                className="grid grid-cols-1 gap-px overflow-hidden rounded-2xl border border-[var(--mk-rule)] bg-[var(--mk-rule)] sm:grid-cols-2 lg:grid-cols-4"
              >
                {row.map((head, i) => {
                  const index = r * 4 + i;
                  const lit = reduced || index === active;
                  const passed = !reduced && index < active;
                  return (
                    <li
                      key={head.id}
                      aria-current={!reduced && index === active ? "step" : undefined}
                      className={cn(
                        "relative bg-white p-5 transition-colors duration-500 motion-reduce:transition-none",
                        lit && !reduced && "bg-[var(--mk-ink)] text-white",
                      )}
                    >
                      <div className="flex items-baseline justify-between gap-3">
                        <span
                          className={cn(
                            "text-sm tabular-nums",
                            lit && !reduced ? "text-[var(--mk-tide-soft)]" : "text-[var(--mk-storm)]",
                          )}
                        >
                          {index + 1}. {head.phase}
                        </span>
                        <span
                          aria-hidden
                          className={cn(
                            "size-2 rounded-full transition-colors duration-500 motion-reduce:transition-none",
                            lit && !reduced ? "bg-[var(--mk-tide-soft)]" : passed ? "bg-[var(--mk-tide)]" : "bg-[var(--mk-rule)]",
                          )}
                        />
                      </div>
                      <h3 className="mk-display mt-6 text-xl">{head.label}</h3>
                      <p
                        className={cn(
                          "mt-2 text-sm leading-relaxed",
                          lit && !reduced ? "text-slate-200" : "text-[var(--mk-storm)]",
                        )}
                      >
                        {head.detail}
                      </p>
                    </li>
                  );
                })}
              </ol>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
