import type { CSSProperties, ElementType, ReactNode } from "react";
import { cn } from "@/lib/utils";

/** Colour of a stamp's paper or of the printed face inside it. Every value is a design token. */
export type StampTone = "paper" | "light" | "dim" | "jade" | "vermilion" | "mustard" | "ink";

const TONE: Record<StampTone, string> = {
  paper: "bg-paper text-ink",
  light: "bg-paper-light text-ink",
  dim: "bg-paper-dim text-ink",
  jade: "bg-jade text-ink",
  vermilion: "bg-vermilion text-paper",
  mustard: "bg-mustard text-ink",
  ink: "bg-ink text-paper",
};

type StampProps = {
  /** Paper colour of the perforated margin. */
  tone?: StampTone;
  /** `sm` punches smaller holes, for grids of many stamps. */
  size?: "md" | "sm";
  /** Rotation in degrees, for a stamp that was pressed on by hand. */
  tilt?: number;
  as?: ElementType;
  className?: string;
  style?: CSSProperties;
  children: ReactNode;
};

/** A postage stamp: paper with a perforated edge (the `.perf` mask in globals.css). Put a `StampFace` inside it. */
export function Stamp({ tone = "paper", size = "md", tilt, as: Tag = "div", className, style, children }: StampProps) {
  return (
    <Tag
      className={cn("perf box-border p-[18px]", size === "sm" && "perf-sm p-3.5", TONE[tone], className)}
      style={tilt ? { transform: `rotate(${tilt}deg)`, ...style } : style}
    >
      {children}
    </Tag>
  );
}

type StampFaceProps = {
  tone?: StampTone;
  as?: ElementType;
  className?: string;
  children: ReactNode;
} & Record<`aria-${string}`, string | undefined>;

/** The inked, bordered picture area inside a `Stamp`. */
export function StampFace({ tone = "paper", as: Tag = "div", className, children, ...aria }: StampFaceProps) {
  return (
    <Tag className={cn("box-border border-2 border-ink p-[18px]", TONE[tone], className)} {...aria}>
      {children}
    </Tag>
  );
}
