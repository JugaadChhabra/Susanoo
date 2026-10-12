"use client";

import { useEffect, useRef, useState, type AnimationEvent } from "react";
import { countUpFrame } from "@/lib/proof";

const COUNT_MS = 600;

/**
 * A Proof "after" number that counts up from its "before" value as its slam starts; a number that didn't change stays
 * put. The label and a screen-reader copy keep the real value, and only the aria-hidden figure counts, over an invisible
 * copy of the final value that holds its width so nothing reflows. Until the slam starts, and with reduced motion or JS
 * off (no slam, or no listener), the figure is the final value. [docs/motion-spec.md §2 Proof, step 4]
 */
export function CountUp({ from, value, label, className }: { from: string; value: string; label: string; className: string }) {
  const [shown, setShown] = useState(value);
  const frame = useRef(0);
  useEffect(() => () => cancelAnimationFrame(frame.current), []);

  function count(event: AnimationEvent) {
    if (event.target !== event.currentTarget || from === value) return;
    cancelAnimationFrame(frame.current);
    const start = performance.now();
    const tick = (now: number) => {
      const t = Math.min((now - start) / COUNT_MS, 1);
      setShown(countUpFrame(from, value, 1 - (1 - t) ** 3));
      if (t < 1) frame.current = requestAnimationFrame(tick);
    };
    tick(start);
  }

  return (
    <span className={className} aria-label={label} onAnimationStart={count}>
      <span className="sr-only">{value}</span>
      <span aria-hidden className="inline-grid tabular-nums">
        <span className="invisible [grid-area:1/1]">{value}</span>
        <span data-count className="[grid-area:1/1]">
          {shown}
        </span>
      </span>
    </span>
  );
}
