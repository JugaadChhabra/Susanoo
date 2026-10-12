import { Postmark, Stamp, StampFace } from "@/components/stamp";

const HEAD_ENDS: [number, number, number][] = [
  [58, 192, -60],
  [110, 142, -80],
  [165, 102, -90],
  [222, 84, -100],
  [282, 100, -110],
  [336, 142, -125],
  [362, 206, -140],
  [256, 228, -130],
];

const NECKS = [
  "M150 345 Q90 280 62 200",
  "M165 345 Q120 230 112 150",
  "M180 345 Q160 200 165 110",
  "M195 345 Q205 180 220 92",
  "M210 345 Q250 200 278 108",
  "M225 345 Q290 240 330 150",
  "M240 345 Q320 290 356 214",
  "M200 345 Q210 280 250 236",
];

/** The hero stamp: an eight-headed serpent rising in front of a red sun. Decorative, described for screen readers. */
export function SerpentStamp() {
  return (
    <div className="relative flex w-full justify-center">
      <Stamp tilt={-2} className="w-full max-w-[460px] p-[22px]">
        <StampFace tone="jade" className="relative flex flex-col gap-2.5">
          <div className="flex items-start justify-between">
            <span className="mk-display text-[30px] tracking-[0.06em] uppercase">Susanoo</span>
            <span
              aria-hidden
              className="vert border-2 border-ink bg-vermilion px-1.5 py-2.5 font-mincho text-2xl font-extrabold text-paper"
            >
              八岐大蛇
            </span>
          </div>
          <svg
            viewBox="0 0 400 360"
            role="img"
            aria-label="An eight-headed serpent rising in front of a red sun and mountains"
            className="-mt-[60px] h-auto w-full"
          >
            <circle cx="250" cy="130" r="110" fill="var(--vermilion)" />
            <path d="M0 300 L80 220 L130 260 L200 190 L290 280 L340 240 L400 290 L400 360 L0 360 Z" fill="var(--paper)" />
            <path d="M200 190 L230 220 L210 225 L190 205 Z" fill="var(--ink)" opacity=".25" />
            <g fill="none" stroke="var(--paper)" strokeWidth="5" strokeLinecap="round" opacity=".9">
              <path d="M20 330 Q60 315 100 330 T180 330" />
              <path d="M220 340 Q260 325 300 340 T380 340" />
            </g>
            <g fill="none" stroke="var(--ink)" strokeWidth="15" strokeLinecap="round">
              {NECKS.map((d) => (
                <path key={d} d={d} />
              ))}
              <path d="M110 352 Q200 320 300 352" strokeWidth="30" />
            </g>
            <g fill="var(--ink)">
              {HEAD_ENDS.map(([cx, cy, rot]) => (
                <ellipse key={`${cx}-${cy}`} cx={cx} cy={cy} rx="17" ry="11" transform={`rotate(${rot} ${cx} ${cy})`} />
              ))}
            </g>
            <g fill="var(--mustard)">
              {HEAD_ENDS.map(([cx, cy]) => (
                <circle key={`${cx}-${cy}`} cx={cx - 4} cy={cy - 6} r="3" />
              ))}
            </g>
          </svg>
          <div className="flex items-end justify-between">
            <span className="max-w-[14em] font-label text-[13px] tracking-[0.12em] uppercase">
              Eight heads · one memory · proven fixes
            </span>
            <span
              aria-hidden
              className="mk-display text-[76px] leading-[0.85] text-mustard [-webkit-text-stroke:2px_var(--ink)]"
            >
              8
            </span>
          </div>
        </StampFace>
      </Stamp>
      <Postmark className="absolute -bottom-20 left-0 sm:-left-8" />
    </div>
  );
}
