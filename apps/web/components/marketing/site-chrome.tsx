import Link from "next/link";
import { FOOTER_LINKS } from "@/lib/marketing";

const NAV = [
  { label: "How it works", href: "/#how-it-works" },
  { label: "Use cases", href: "/#use-cases" },
  { label: "Trust", href: "/#trust" },
  { label: "Connectors", href: "/#connectors" },
  { label: "Pricing", href: "/#pricing" },
];

export const FOCUS_RING =
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--mk-tide)]";

/** Focus ring for links on the dark ink background. */
export const FOCUS_RING_DARK =
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--mk-tide-soft)]";

/** Kept in the footer because the header nav is hidden at phone width. */
const FOOTER_PAGES = [
  { label: "Pricing", href: "/#pricing" },
  { label: "Watch the demo", href: "/demo" },
];

export function Wordmark() {
  return (
    <span className="flex items-center gap-3">
      <span
        aria-hidden
        className="vert rounded-[2px] bg-vermilion px-1 py-1.5 font-mincho text-[15px] leading-[1.05] font-extrabold text-paper"
      >
        須佐
      </span>
      <span className="mk-display text-[26px] tracking-[0.04em] uppercase">Susanoo</span>
    </span>
  );
}

export function SiteHeader() {
  return (
    <header className="border-b-[3px] border-ink">
      <div className="mx-auto flex w-full max-w-[1280px] flex-wrap items-center gap-x-7 gap-y-3 px-[clamp(20px,4vw,48px)] py-[18px]">
        <Link href="/" className={`rounded-[2px] ${FOCUS_RING}`} aria-label="Susanoo home">
          <Wordmark />
        </Link>
        <nav
          aria-label="Main"
          className="hidden flex-1 flex-wrap gap-6 text-base font-medium tracking-[0.08em] text-ink uppercase md:flex"
        >
          {NAV.map((item) => (
            <Link key={item.href} href={item.href} className={`rounded-sm hover:text-vermilion ${FOCUS_RING}`}>
              {item.label}
            </Link>
          ))}
        </nav>
        <div className="ml-auto flex items-center gap-3 text-base">
          <Link href="/login" className={`rounded-[2px] px-4 py-3 font-semibold hover:text-vermilion ${FOCUS_RING}`}>
            Sign in
          </Link>
          <Link
            href="/projects/new"
            className={`mk-btn rounded-[2px] bg-ink px-5 py-3 font-semibold text-paper hover:bg-vermilion ${FOCUS_RING}`}
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
    <footer className="bg-ink text-paper">
      <div className="mx-auto flex w-full max-w-[1280px] flex-col gap-8 px-[clamp(20px,4vw,48px)] py-14 md:flex-row md:items-start md:justify-between">
        <div className="max-w-md">
          <div className="flex items-baseline gap-3 text-paper">
            <Wordmark />
            <span aria-hidden className="font-mincho text-[26px] font-extrabold text-mustard">
              須佐之男
            </span>
          </div>
        </div>
        <nav aria-label="Footer">
          <ul className="flex flex-wrap gap-x-6 gap-y-3 text-base">
            {FOOTER_PAGES.map((page) => (
              <li key={page.href}>
                <Link href={page.href} className={`rounded-sm text-paper underline-offset-4 hover:underline ${FOCUS_RING_DARK}`}>
                  {page.label}
                </Link>
              </li>
            ))}
            {FOOTER_LINKS.map((link) => (
              <li key={link.href}>
                <a
                  href={link.href}
                  target="_blank"
                  rel="noopener noreferrer"
                  className={`rounded-sm text-paper underline-offset-4 hover:underline ${FOCUS_RING_DARK}`}
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

/** Stamp palette and fonts, skip link, header and footer shared by every marketing page. */
export function MarketingShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="mk dots flex min-h-full flex-1 flex-col overflow-x-clip">
      <a
        href="#main"
        className="sr-only z-50 rounded-[2px] bg-paper px-4 py-2 focus:not-sr-only focus:fixed focus:top-3 focus:left-3"
      >
        Skip to content
      </a>
      <SiteHeader />
      <main id="main" className="flex-1">
        {children}
      </main>
      <SiteFooter />
    </div>
  );
}
