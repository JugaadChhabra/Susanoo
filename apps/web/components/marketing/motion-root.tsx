"use client";

import { useEffect } from "react";

/**
 * Switches marketing motion on when reduced motion isn't requested, and marks each [data-reveal] as in view once, so
 * its paused .mk-anim animations play a single time. Without it nothing is paused. [docs/motion-spec.md §4]
 */
export function MotionRoot() {
  useEffect(() => {
    if (!window.matchMedia("(prefers-reduced-motion: no-preference)").matches) return;
    const root = document.documentElement;
    root.dataset.motion = "on";
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          const el = entry.target as HTMLElement;
          if (entry.isIntersecting) {
            el.dataset.inview = "";
            observer.unobserve(el);
          } else if (entry.boundingClientRect.top >= window.innerHeight) {
            // Before hydration nothing was paused, so rewind what already played out of sight to its first frame,
            // including anything in its [data-reveal-scope] that waits on it.
            const scope = el.closest("[data-reveal-scope]") ?? el;
            for (const animation of scope.getAnimations({ subtree: true })) animation.currentTime = 0;
          }
        }
      },
      { rootMargin: "0px 0px -12% 0px", threshold: 0.15 },
    );
    document.querySelectorAll("[data-reveal]").forEach((el) => observer.observe(el));
    return () => {
      observer.disconnect();
      delete root.dataset.motion;
    };
  }, []);
  return null;
}
