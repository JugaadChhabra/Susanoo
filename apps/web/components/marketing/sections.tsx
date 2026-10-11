import Link from "next/link";
import { cn } from "@/lib/utils";
import { CONNECTORS, TRUST_POINTS, USE_CASES } from "@/lib/marketing";
import { FOCUS_RING } from "./site-chrome";

const PRIMARY = `inline-flex h-12 items-center justify-center rounded-full bg-[var(--mk-ink)] px-6 font-medium text-white hover:bg-[var(--mk-storm)] ${FOCUS_RING}`;
const SECONDARY = `inline-flex h-12 items-center justify-center rounded-full border border-[var(--mk-ink)] px-6 font-medium text-[var(--mk-ink)] hover:bg-white ${FOCUS_RING}`;

/** The spec from the hero use case, word for word. [PRD › Hero use case: the code review agent] */
const EXAMPLE_SPEC =
  "Build a code review app for our Python services. Developers paste a diff or a PR link and get a review. Rules: never log PII; every FastAPI endpoint needs auth; no raw SQL string formatting; no secrets in code; flag missing error handling on external calls. Severity: block, warn, or nit.";

function StatusTag({ live, label }: { live: boolean; label: string }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium",
        live ? "bg-[var(--mk-tide)] text-white" : "border border-[var(--mk-amber)] text-[var(--mk-amber)]",
      )}
    >
      {live && <span aria-hidden className="size-1.5 rounded-full bg-white" />}
      {label}
    </span>
  );
}

export function Hero() {
  return (
    <section aria-labelledby="hero-heading" className="mx-auto grid w-full max-w-6xl gap-12 px-4 pt-16 pb-20 sm:px-6 sm:pt-24 lg:grid-cols-[1.15fr_1fr] lg:gap-16 lg:pb-28">
      <div>
        <h1 id="hero-heading" className="mk-display text-[2.6rem] leading-[1.02] sm:text-6xl lg:text-7xl">
          AI agents are a many-headed mess. Susanoo tames them.
        </h1>
        <p className="mt-6 max-w-xl text-lg leading-relaxed text-[var(--mk-storm)]">
          Describe the agent you need in plain English. Susanoo builds it into a working app, attacks it with test cases,
          works out why it fails, fixes the part that broke and proves the fix with traces you can open.
        </p>
        <div className="mt-8 flex flex-col gap-3 sm:flex-row">
          <Link href="/projects/new" className={PRIMARY}>
            Start building
          </Link>
          <Link href="/demo" className={SECONDARY}>
            Watch the demo
          </Link>
        </div>
      </div>

      <figure className="self-end rounded-2xl border border-[var(--mk-rule)] bg-white p-6 shadow-[0_24px_60px_-30px_rgb(20_32_43/0.35)] sm:p-8">
        <figcaption className="text-sm text-[var(--mk-storm)]">You write a spec like this one</figcaption>
        <blockquote className="mt-4 text-[1.05rem] leading-relaxed text-[var(--mk-ink)]">{EXAMPLE_SPEC}</blockquote>
        <p className="mt-6 border-t border-[var(--mk-rule)] pt-4 text-sm leading-relaxed text-[var(--mk-storm)]">
          You get a code reviewer with its own web page and repo, tested against diffs with planted bugs, and a report of
          what it caught before and after each fix.
        </p>
      </figure>
    </section>
  );
}

function SectionIntro({ id, title, children }: { id: string; title: string; children: React.ReactNode }) {
  return (
    <div className="max-w-2xl">
      <h2 id={id} className="mk-display text-3xl leading-tight sm:text-5xl">
        {title}
      </h2>
      <p className="mt-4 text-lg leading-relaxed text-[var(--mk-storm)]">{children}</p>
    </div>
  );
}

export function UseCases() {
  const live = USE_CASES.filter((u) => u.live);
  const later = USE_CASES.filter((u) => !u.live);
  return (
    <section id="use-cases" aria-labelledby="use-cases-heading" className="border-t border-[var(--mk-rule)] py-20 sm:py-28">
      <div className="mx-auto w-full max-w-6xl px-4 sm:px-6">
        <SectionIntro id="use-cases-heading" title="Start with code review">
          Any team can turn a manual workflow into an agent they can trust. We started where the answers are easiest to
          check: reviewing code against your team&apos;s rules.
        </SectionIntro>
        <div className="mt-12 grid gap-6 lg:grid-cols-[1.3fr_1fr]">
          <div className="grid gap-6">
            {live.map((u) => (
              <article key={u.title} className="rounded-2xl bg-[var(--mk-ink)] p-8 text-white">
                <StatusTag live label="Live now" />
                <h3 className="mk-display mt-6 text-3xl">{u.title}</h3>
                <p className="mt-3 max-w-md leading-relaxed text-slate-300">{u.body}</p>
                <Link
                  href="/projects/new"
                  className="mt-8 inline-flex h-11 items-center rounded-full bg-white px-5 text-sm font-medium text-[var(--mk-ink)] hover:bg-[var(--mk-mist)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--mk-tide-soft)]"
                >
                  Start building
                </Link>
              </article>
            ))}
          </div>
          <ul className="divide-y divide-[var(--mk-rule)] rounded-2xl border border-[var(--mk-rule)] bg-white">
            {later.map((u) => (
              <li key={u.title} className="p-6">
                <div className="flex items-center justify-between gap-3">
                  <h3 className="mk-display text-xl">{u.title}</h3>
                  <StatusTag live={false} label="Coming soon" />
                </div>
                <p className="mt-2 text-sm leading-relaxed text-[var(--mk-storm)]">{u.body}</p>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </section>
  );
}

export function Trust() {
  return (
    <section id="trust" aria-labelledby="trust-heading" className="border-t border-[var(--mk-rule)] py-20 sm:py-28">
      <div className="mx-auto grid w-full max-w-6xl gap-12 px-4 sm:px-6 lg:grid-cols-[1fr_1.4fr]">
        <SectionIntro id="trust-heading" title="Built so it can't fool you">
          An agent that fixes itself could easily learn to pass its own tests. These rules stop that.
        </SectionIntro>
        <dl className="grid gap-x-10 gap-y-10 sm:grid-cols-2">
          {TRUST_POINTS.map((t) => (
            <div key={t.title} className="border-t-2 border-[var(--mk-tide)] pt-4">
              <dt className="mk-display text-xl">{t.title}</dt>
              <dd className="mt-2 leading-relaxed text-[var(--mk-storm)]">{t.body}</dd>
            </div>
          ))}
        </dl>
      </div>
    </section>
  );
}

export function Connectors() {
  return (
    <section id="connectors" aria-labelledby="connectors-heading" className="border-t border-[var(--mk-rule)] py-20 sm:py-28">
      <div className="mx-auto w-full max-w-6xl px-4 sm:px-6">
        <SectionIntro id="connectors-heading" title="Plug into the tools you already use">
          Connect an account in a few guided steps. Susanoo checks the connection straight away and never shows your key
          again.
        </SectionIntro>
        <ul className="mt-12 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {CONNECTORS.map((c) => (
            <li
              key={c.name}
              className={cn(
                "rounded-2xl border p-6",
                c.live ? "border-[var(--mk-ink)] bg-white" : "border-dashed border-[var(--mk-rule)]",
              )}
            >
              <div className="flex items-center justify-between gap-3">
                <h3 className={cn("mk-display text-xl", !c.live && "text-[var(--mk-storm)]")}>{c.name}</h3>
                <StatusTag live={c.live} label={c.live ? "Available" : "On the roadmap"} />
              </div>
              <p className="mt-2 text-sm leading-relaxed text-[var(--mk-storm)]">{c.body}</p>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}

export function Plans() {
  return (
    <section id="pricing" aria-labelledby="pricing-heading" className="border-t border-[var(--mk-rule)] py-16">
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 px-4 sm:px-6 md:flex-row md:items-center md:justify-between">
        <div>
          <h2 id="pricing-heading" className="mk-display text-2xl sm:text-3xl">
            Free, Team and Enterprise plans
          </h2>
          <p className="mt-2 text-[var(--mk-storm)]">Try it free. Larger teams get private repos, custom rules, SSO and support.</p>
        </div>
        <Link href="/pricing" className={SECONDARY}>
          Compare plans
        </Link>
      </div>
    </section>
  );
}

export function DemoBand() {
  return (
    <section id="demo" aria-labelledby="demo-heading" className="bg-[var(--mk-storm)] py-20 text-white">
      {/* #19 replaces this band with the embedded replay of the saved run. */}
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 px-4 sm:px-6 md:flex-row md:items-center md:justify-between">
        <div className="max-w-xl">
          <h2 id="demo-heading" className="mk-display text-3xl sm:text-4xl">
            Watch a whole run, start to finish
          </h2>
          <p className="mt-3 leading-relaxed text-slate-200">
            A replay of a real run: the heads build a reviewer, catch it missing bugs, fix it and prove the fix. No sign-in
            needed.
          </p>
        </div>
        <Link
          href="/demo"
          className="inline-flex h-12 w-fit items-center rounded-full bg-white px-6 font-medium text-[var(--mk-ink)] hover:bg-[var(--mk-mist)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white"
        >
          Watch the demo
        </Link>
      </div>
    </section>
  );
}
