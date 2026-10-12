import { cn } from "@/lib/utils";

type PostmarkProps = {
  /** Text on the circle, upper line. */
  top?: string;
  /** Text on the circle, lower line (usually the year). */
  bottom?: string;
  /** Rotation in degrees. */
  rotate?: number;
  className?: string;
};

/** A cancellation postmark: a double ring with two lines of text and three wavy lines. Decorative. */
export function Postmark({ top = "NEATHACK", bottom = "2026", rotate = -12, className }: PostmarkProps) {
  const wave = "q15 -10 30 0 t30 0 t30 0 t30 0";
  return (
    <svg
      viewBox="0 0 260 140"
      aria-hidden
      className={cn("pointer-events-none w-[260px] opacity-80", className)}
      style={{ transform: `rotate(${rotate}deg)` }}
    >
      <circle cx="70" cy="70" r="58" fill="none" stroke="var(--ink)" strokeWidth="3" />
      <circle cx="70" cy="70" r="42" fill="none" stroke="var(--ink)" strokeWidth="2" />
      <text x="70" y="64" textAnchor="middle" fontFamily="var(--font-label)" fontSize="13" fill="var(--ink)">
        {top}
      </text>
      <text x="70" y="84" textAnchor="middle" fontFamily="var(--font-label)" fontSize="15" fontWeight="500" fill="var(--ink)">
        {bottom}
      </text>
      <g fill="none" stroke="var(--ink)" strokeWidth="3">
        <path d={`M135 48 ${wave}`} />
        <path d={`M135 70 ${wave}`} />
        <path d={`M135 92 ${wave}`} />
      </g>
    </svg>
  );
}
