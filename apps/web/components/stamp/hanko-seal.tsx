import { cn } from "@/lib/utils";

type HankoShape = "square" | "round" | "solid" | "double";

type HankoSealProps = {
  /** One kanji, carved into the seal (or, with `label`, a short Japanese word). */
  char: string;
  /** Outline shape of a single-character seal. Ignored when `label` is set. */
  shape?: HankoShape;
  /** With a label the seal becomes a rectangular plate: the Japanese word above the Latin one. */
  label?: string;
  /** Rotation in degrees. */
  rotate?: number;
  className?: string;
};

const SHAPE: Record<HankoShape, string> = {
  square: "rounded-md border-[5px] border-vermilion text-vermilion",
  round: "rounded-full border-[5px] border-vermilion text-vermilion",
  solid: "rounded-md bg-vermilion text-paper",
  double: "rounded-md border-[5px] border-double border-vermilion text-vermilion",
};

/** A red hanko (name seal). Decorative: the meaning is always spelled out in the text next to it. */
export function HankoSeal({ char, shape = "square", label, rotate = 0, className }: HankoSealProps) {
  const style = { transform: `rotate(${rotate}deg)` };
  if (label) {
    return (
      <span
        aria-hidden
        className={cn(
          "inline-flex flex-col items-center border-4 border-vermilion bg-paper/60 px-3.5 py-2 text-vermilion",
          className,
        )}
        style={style}
      >
        <span className="font-mincho text-[26px] leading-none font-extrabold">{char}</span>
        <span className="font-stamp text-sm tracking-[0.12em]">{label}</span>
      </span>
    );
  }
  return (
    <span
      aria-hidden
      className={cn(
        "flex size-[84px] items-center justify-center font-mincho text-5xl font-extrabold",
        SHAPE[shape],
        className,
      )}
      style={style}
    >
      {char}
    </span>
  );
}
