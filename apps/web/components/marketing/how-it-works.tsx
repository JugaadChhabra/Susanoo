"use client";

import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { Pause, Play } from "lucide-react";
import { Stamp, StampFace, StampSheet } from "@/components/stamp";
import { cn } from "@/lib/utils";
import { HEAD_STEPS, nextHeadIndex } from "@/lib/marketing";

const STEP_MS = 1800;

/** Kanji numerals 1 to 8, printed on each head's stamp. */
const NUMERALS = ["壱", "弐", "参", "肆", "伍", "陸", "漆", "捌"];

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
  const [onScreen, setOnScreen] = useState(false);
  const sectionRef = useRef<HTMLElement>(null);
  const playing = !paused && !reduced && onScreen;

  // Only step while the section is on screen.
  useEffect(() => {
    const el = sectionRef.current;
    if (!el) return;
    const observer = new IntersectionObserver(([entry]) => setOnScreen(entry.isIntersecting));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!playing) return;
    const timer = window.setInterval(() => setActive(nextHeadIndex), STEP_MS);
    return () => window.clearInterval(timer);
  }, [playing]);

  const rows = [HEAD_STEPS.slice(0, 4), HEAD_STEPS.slice(4)];

  return (
    <section
      ref={sectionRef}
      id="how-it-works"
      aria-labelledby="how-heading"
      className="mx-auto flex w-full max-w-[1280px] flex-col gap-10 px-[clamp(20px,4vw,48px)] py-20 sm:py-[104px]"
    >
      <div className="flex flex-wrap items-end justify-between gap-6">
        <div className="flex max-w-[640px] flex-col gap-3">
          <h2 id="how-heading" className="mk-display text-[clamp(32px,4vw,52px)] leading-[1.04]">
            Eight heads, one job each
          </h2>
          <p className="text-[19px] leading-relaxed">
            In the old story, Susanoo defeated Orochi, a serpent with eight heads. Here the eight heads work for you. They
            share one memory of the run and hand off to each other in order.
          </p>
        </div>
        {!reduced && (
          <button
            type="button"
            onClick={() => setPaused((p) => !p)}
            className="mk-btn mk-btn-vermilion inline-flex h-11 w-fit items-center gap-2 rounded-[2px] border-2 border-ink bg-paper px-4 text-base font-semibold text-ink hover:bg-mustard focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-vermilion"
          >
            {paused ? <Play className="size-4" aria-hidden /> : <Pause className="size-4" aria-hidden />}
            {paused ? "Play the sequence" : "Pause the sequence"}
          </button>
        )}
      </div>

      <div className="flex flex-col gap-10">
        {rows.map((row, r) => (
          <div key={r}>
            <p className="mb-3 flex items-center gap-2.5 text-base font-semibold">
              <span aria-hidden className={cn("size-[18px] border-2 border-ink", r === 0 ? "bg-jade" : "bg-vermilion")} />
              {r === 0
                ? "First, it builds"
                : "Then it loops: up to five rounds, stopping early when two rounds in a row don't improve"}
            </p>
            <StampSheet as="ol" className="m-0 list-none">
              {row.map((head, i) => {
                const index = r * 4 + i;
                const lit = !reduced && index === active;
                const passed = !reduced && index < active;
                return (
                  <li key={head.id} aria-current={lit ? "step" : undefined} className="min-w-0">
                    <Stamp size="sm" tone="light" className="h-full">
                      <StampFace
                        as="article"
                        tone={lit ? "mustard" : r === 0 ? "jade" : "vermilion"}
                        className="flex h-full min-h-[250px] flex-col gap-2.5 p-4 transition-colors duration-500 motion-reduce:transition-none"
                      >
                        <div className="flex items-start justify-between">
                          <span className="font-label text-[13px] tracking-[0.14em] uppercase tabular-nums">
                            {index + 1}. {head.phase}
                          </span>
                          <span aria-hidden className="font-mincho text-[40px] leading-none font-extrabold">
                            {NUMERALS[index]}
                          </span>
                        </div>
                        <h3 className="mk-display mt-2 text-[22px] leading-[1.1] break-words sm:text-2xl">{head.label}</h3>
                        <p className="text-base leading-[1.45]">{head.detail}</p>
                        <span
                          aria-hidden
                          className={cn(
                            "mt-auto size-2.5 self-end rounded-full border-2 border-ink transition-colors duration-500 motion-reduce:transition-none",
                            lit ? "bg-ink" : passed ? "bg-paper" : "bg-transparent",
                          )}
                        />
                      </StampFace>
                    </Stamp>
                  </li>
                );
              })}
            </StampSheet>
          </div>
        ))}
      </div>
    </section>
  );
}
