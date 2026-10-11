import Link from "next/link";
import { FOOTER_LINKS } from "@/lib/marketing";

const NAV = [
  { label: "How it works", href: "#how-it-works" },
  { label: "Use cases", href: "#use-cases" },
  { label: "Trust", href: "#trust" },
  { label: "Connectors", href: "#connectors" },
  { label: "Pricing", href: "/pricing" },
];

export const FOCUS_RING =
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--mk-tide)]";

export function Wordmark() {
  return (
    <span className="mk-display flex items-center gap-2 text-lg">
      <svg viewBox="0 0 24 24" className="size-6" aria-hidden>
        {/* Eight strokes gathered into one: the heads, tamed. */}
        {Array.from({ length: 8 }, (_, i) => (
          <path
            key={i}
            d={`M12 21 C 12 15, ${3 + i * 2.57} 9, ${3 + i * 2.57} 3`}
            fill="none"
            stroke="currentColor"
            strokeWidth="1.4"
            strokeLinecap="round"
          />
        ))}
      </svg>
      Susanoo
    </span>
  );
}

export function SiteHeader() {
  return (
    <header className="border-b border-[var(--mk-rule)]">
      <div className="mx-auto flex w-full max-w-6xl items-center justify-between gap-4 px-4 py-4 sm:px-6">
        <Link href="/" className={`rounded-md ${FOCUS_RING}`} aria-label="Susanoo home">
          <Wordmark />
        </Link>
        <nav aria-label="Main" className="hidden items-center gap-6 text-sm text-[var(--mk-storm)] md:flex">
          {NAV.map((item) => (
            <a key={item.href} href={item.href} className={`rounded-sm hover:text-[var(--mk-ink)] ${FOCUS_RING}`}>
              {item.label}
            </a>
          ))}
        </nav>
        <div className="flex items-center gap-2 text-sm">
          <Link href="/login" className={`rounded-full px-3 py-2 text-[var(--mk-storm)] hover:text-[var(--mk-ink)] ${FOCUS_RING}`}>
            Sign in
          </Link>
          <Link
            href="/projects/new"
            className={`rounded-full bg-[var(--mk-ink)] px-4 py-2 font-medium text-white hover:bg-[var(--mk-storm)] ${FOCUS_RING}`}
          >
            Start building
          </Link>
        </div>
      </div>
    </header>
  );
}

export function SiteFooter() {
  return (
    <footer className="border-t border-[var(--mk-rule)] bg-[var(--mk-ink)] text-slate-300">
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-10 px-4 py-14 sm:px-6 md:flex-row md:justify-between">
        <div className="max-w-sm">
          <div className="text-white">
            <Wordmark />
          </div>
          <p className="mt-4 text-sm leading-relaxed">
            Built in a weekend for neatHack 2026 by Muaaz Shaikh and Jugaad Chhabra. The code is open source.
          </p>
        </div>
        <nav aria-label="Footer">
          <ul className="grid gap-3 text-sm sm:grid-cols-2 sm:gap-x-12">
            {FOOTER_LINKS.map((link) => (
              <li key={link.href}>
                <a
                  href={link.href}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="rounded-sm text-white underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--mk-tide-soft)]"
                >
                  {link.label}
                </a>
              </li>
            ))}
          </ul>
        </nav>
      </div>
    </footer>
  );
}
