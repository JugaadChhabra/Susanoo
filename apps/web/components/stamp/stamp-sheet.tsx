import type { ElementType, ReactNode } from "react";
import { cn } from "@/lib/utils";

type StampSheetProps = {
  /** Use `ol` or `ul` when the children are `li` elements. */
  as?: ElementType;
  /** Narrowest a stamp may get before the grid wraps, in px. */
  minColumn?: number;
  className?: string;
  children: ReactNode;
};

/** A sheet of stamps: dashed-edge paper laid out as a responsive grid. */
export function StampSheet({ as: Tag = "div", minColumn = 240, className, children }: StampSheetProps) {
  return (
    <Tag
      className={cn("grid gap-6 border-2 border-dashed border-ink/40 bg-paper p-5 sm:p-7", className)}
      style={{ gridTemplateColumns: `repeat(auto-fit, minmax(min(${minColumn}px, 100%), 1fr))` }}
    >
      {children}
    </Tag>
  );
}
