import Link from "next/link";
import { cn } from "@/lib/utils";
import { DemoReplay } from "@/components/app/demo-replay";
import { CONNECTORS, REPO_URL, TRUST_POINTS, USE_CASES } from "@/lib/marketing";
import { PROOF_RUN, proofStats, traceUrl } from "@/lib/proof";
import { HankoSeal, Stamp, StampFace } from "@/components/stamp";
import { SerpentStamp } from "./serpent-stamp";
import { FOCUS_RING } from "./site-chrome";

const PRIMARY = `inline-flex items-center justify-center rounded-[2px] bg-vermilion px-7 py-4 text-lg font-semibold text-paper hover:bg-ink ${FOCUS_RING}`;
const SECONDARY = `inline-flex items-center justify-center rounded-[2px] border-2 border-ink px-[26px] py-3.5 text-lg font-semibold text-ink hover:bg-paper ${FOCUS_RING}`;

/** Page gutters shared by every section. */
const GUTTER = "mx-auto w-full max-w-[1280px] px-[clamp(20px,4vw,48px)]";

/** The spec from the hero use case, word for word. [PRD › Hero use case: the code review agent] */
const EXAMPLE_SPEC =
  "Build a code review app for our Python services. Developers paste a diff or a PR link and get a review. Rules: never log PII; every FastAPI endpoint needs auth; no raw SQL string formatting; no secrets in code; flag missing error handling on external calls. Severity: block, warn, or nit.";

function StatusTag({ live, label }: { live: boolean; label: string }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 self-start px-2 py-0.5 font-label text-xs tracking-[0.12em] uppercase",
        live ? "bg-vermilion text-paper" : "border border-ink text-ink",
      )}
    >
      {live && <span aria-hidden className="size-1.5 rounded-full bg-paper" />}
      {label}
    </span>
  );
}

export function Hero() {
  return (
    <section
      aria-labelledby="hero-heading"
      className={`${GUTTER} flex flex-wrap items-center gap-16 pt-16 pb-20 sm:pt-[88px] sm:pb-24`}
    >
      <div className="flex min-w-0 flex-[1_1_520px] gap-8">
        <div
          aria-hidden
          className="vert hidden self-start border-[3px] border-vermilion px-2 py-3.5 font-mincho text-[30px] leading-[1.1] font-extrabold text-vermilion sm:block"
        >
          八岐退治
        </div>
        <div className="flex min-w-0 flex-col gap-7">
          <h1
            id="hero-heading"
            className="mk-display text-[clamp(34px,4.4vw,60px)] leading-[1.02] [overflow-wrap:anywhere] sm:[overflow-wrap:normal]"
          >
            AI agents are a many-headed mess. <span className="text-vermilion">Susanoo tames them.</span>
          </h1>
          <p className="max-w-[34em] text-[21px] leading-normal">
            Describe the agent you need in plain English. Susanoo builds it into a working app, attacks it with test cases,
            works out why it fails, fixes the part that broke and proves the fix with traces.
          </p>
          <div className="flex flex-wrap gap-3.5">
            <Link href="/projects/new" className={PRIMARY}>
              Start building
            </Link>
            <Link href="/demo" className={SECONDARY}>
              Watch the demo
            </Link>
          </div>
        </div>
      </div>
      <div className="min-w-0 flex-[1_1_420px]">
        <SerpentStamp />
      </div>
    </section>
  );
}

/** The example spec and what comes back, set as a telegram on a dark band. */
export function Telegram() {
  return (
    <section aria-labelledby="spec-heading" className="bg-ink px-[clamp(20px,4vw,48px)] py-20 text-paper">
      <div className="mx-auto flex max-w-[1280px] flex-wrap items-center gap-12">
        <div className="flex flex-[1_1_340px] flex-col gap-4">
          <h2 id="spec-heading" className="mk-display text-[clamp(30px,3.4vw,42px)] leading-[1.08]">
            You write a spec like this one
          </h2>
          <p className="text-[19px] text-ink-on-dark">
            You get a code reviewer with its own web page and repo, tested against diffs with planted bugs, and a report of
            what it caught before and after each fix.
          </p>
        </div>
        <figure
          aria-labelledby="spec-heading"
          className="m-0 min-w-0 flex-[1_1_560px] border-2 border-paper bg-paper text-ink shadow-[10px_10px_0_var(--vermilion)]"
        >
          <div className="flex items-center justify-between gap-3 bg-vermilion px-5 py-2.5 text-paper">
            <span aria-hidden className="mk-display text-lg tracking-[0.1em]">
              SPEC FORM 01
            </span>
            <span aria-hidden className="font-label text-[13px]">
              TO: SUSANOO · FROM: YOUR TEAM
            </span>
          </div>
          <blockquote
            className="m-0 px-6 py-[22px] font-label text-base leading-8"
            style={{ backgroundImage: "repeating-linear-gradient(transparent 0 31px, rgb(27 26 23 / 0.18) 31px 32px)" }}
          >
            {EXAMPLE_SPEC}
          </blockquote>
        </figure>
      </div>
    </section>
  );
}

function SectionIntro({
  id,
  title,
  children,
}: {
  id: string;
  title: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="flex max-w-[700px] flex-col gap-3">
      <h2 id={id} className="mk-display text-[clamp(32px,4vw,52px)] leading-[1.04]">
        {title}
      </h2>
      {children && <p className="text-[19px] leading-relaxed">{children}</p>}
    </div>
  );
}

export function Proof() {
  const stats = proofStats(PROOF_RUN.before, PROOF_RUN.after);
  const template = process.env.NEXT_PUBLIC_NEATLOGS_TRACE_URL_TEMPLATE;
  return (
    <section
      id="proof"
      aria-labelledby="proof-heading"
      className="border-y-[3px] border-ink bg-jade px-[clamp(20px,4vw,48px)] py-20 sm:py-24"
    >
      <div className="mx-auto flex max-w-[1280px] flex-col gap-11">
        <div className="relative">
          <SectionIntro id="proof-heading" title="Before and after a fix, from a real run">
            In a test round, the code reviewer Susanoo built let an access token typed into code slip through. Susanoo
            traced the miss to the agent&apos;s instructions, changed three lines and kept the change only after every test
            was rerun. These are the scores either side of that fix.
          </SectionIntro>
          <HankoSeal char="検証済" label="VERIFIED" rotate={10} className="absolute top-0 right-2 hidden lg:inline-flex" />
        </div>
        <dl className="grid gap-8 sm:grid-cols-3">
          {stats.map((s) => (
            <Stamp key={s.label} className="p-4">
              <StampFace className="flex h-full flex-col gap-2.5 p-6">
                <dt className="font-label text-[13px] tracking-[0.14em] uppercase">{s.label}</dt>
                <dd className="m-0 flex flex-wrap items-baseline gap-3">
                  <span className="font-label text-xl line-through decoration-1" aria-label={`before ${s.before}`}>
                    {s.before}
                  </span>
                  <span aria-hidden>→</span>
                  <span className="mk-display text-5xl text-vermilion" aria-label={`after ${s.after}`}>
                    {s.after}
                  </span>
                </dd>
                <dd className="m-0 text-base leading-snug font-medium">{s.note}</dd>
              </StampFace>
            </Stamp>
          ))}
        </dl>
        <p className="max-w-3xl text-base leading-relaxed">
          Both rounds score the same 28 tests. No new false alarms, and the hidden-test score held steady rather than
          rising: the one hidden test it still misses stays hidden from the heads that fix it, so they can&apos;t tune
          the agent to it. The whole run cost ${PROOF_RUN.totalCostUsd.toFixed(2)} in model calls.
        </p>
        <div>
          <ul className="flex flex-col gap-3 sm:flex-row sm:flex-wrap">
            {PROOF_RUN.traces.map((t) => {
              const href = traceUrl(template, t.traceId);
              return (
                <li key={t.traceId} className="text-sm">
                  {href ? (
                    <a
                      href={href}
                      target="_blank"
                      rel="noreferrer"
                      className={`inline-flex min-h-11 items-center rounded-[2px] border-2 border-ink bg-paper px-4 font-semibold text-ink hover:bg-ink hover:text-paper ${FOCUS_RING}`}
                    >
                      {t.label}: open the neatlogs trace
                      <span className="sr-only"> (opens neatlogs; needs a team sign-in)</span>
                    </a>
                  ) : (
                    <span>
                      {t.label}: neatlogs trace <code className="font-label text-xs break-all">{t.traceId}</code>
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
          <p className="mt-3 max-w-3xl text-sm leading-relaxed">
            neatlogs traces are private to the Susanoo team&apos;s workspace, so the links ask for a sign-in. Screenshots of
            both traces are in{" "}
            <a
              href={`${REPO_URL}/blob/main/EVIDENCE.md`}
              target="_blank"
              rel="noreferrer"
              className={`underline underline-offset-2 hover:text-vermilion ${FOCUS_RING}`}
            >
              the repo&apos;s evidence file
            </a>
            .
          </p>
        </div>
      </div>
    </section>
  );
}

export function UseCases() {
  const live = USE_CASES.filter((u) => u.live);
  const later = USE_CASES.filter((u) => !u.live);
  return (
    <section id="use-cases" aria-labelledby="use-cases-heading" className={`${GUTTER} flex flex-col gap-10 py-20 sm:py-[104px]`}>
      <SectionIntro id="use-cases-heading" title="Start with code review">
        Any team can turn a manual workflow into an agent they can trust. We started where the answers are easiest to
        check: reviewing code against your team&apos;s rules.
      </SectionIntro>
      <div className="flex flex-wrap gap-7">
        <div className="grid flex-[2_1_460px] gap-7">
          {live.map((u) => (
            <Stamp key={u.title}>
              <StampFace tone="vermilion" as="article" className="flex h-full flex-col gap-4 p-8">
                <div className="flex items-start justify-between">
                  <StatusTag live label="Live now" />
                  <span aria-hidden className="font-mincho text-[30px] leading-none font-extrabold">
                    査読
                  </span>
                </div>
                <h3 className="mk-display text-[40px] leading-tight">{u.title}</h3>
                <p className="max-w-[30em] text-lg">{u.body}</p>
                <Link
                  href="/projects/new"
                  className="mt-auto inline-flex w-fit items-center rounded-[2px] bg-paper px-6 py-3.5 font-semibold text-ink hover:bg-mustard focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-mustard"
                >
                  Start building
                </Link>
              </StampFace>
            </Stamp>
          ))}
        </div>
        <ul className="m-0 flex flex-[1_1_300px] list-none flex-col gap-4 p-0">
          {later.map((u) => (
            <li key={u.title} className="flex flex-col gap-1 border-2 border-dashed border-ink p-5 opacity-80">
              <StatusTag live={false} label="Coming soon" />
              <h3 className="mk-display mt-1 text-2xl">{u.title}</h3>
              <p className="text-base">{u.body}</p>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}

/** One seal per trust point, in the order of TRUST_POINTS. */
const SEALS: { char: string; shape: "square" | "round" | "solid" | "double"; rotate: number }[] = [
  { char: "隠", shape: "square", rotate: -4 },
  { char: "承", shape: "round", rotate: 3 },
  { char: "封", shape: "solid", rotate: -2 },
  { char: "待", shape: "double", rotate: 5 },
];

export function Trust() {
  return (
    <section
      id="trust"
      aria-labelledby="trust-heading"
      className="border-y-[3px] border-ink bg-paper px-[clamp(20px,4vw,48px)] py-20 sm:py-24"
    >
      <div className="mx-auto flex max-w-[1280px] flex-col gap-11">
        <SectionIntro id="trust-heading" title="Built so it can't fool you">
          An agent that fixes itself could easily learn to pass its own tests. These rules stop that.
        </SectionIntro>
        <dl className="grid gap-9" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(250px, 100%), 1fr))" }}>
          {TRUST_POINTS.map((t, i) => {
            const seal = SEALS[i % SEALS.length];
            return (
              <div key={t.title} className="flex flex-col gap-3.5">
                <HankoSeal char={seal.char} shape={seal.shape} rotate={seal.rotate} />
                <dt className="mk-display text-[22px] leading-tight">{t.title}</dt>
                <dd className="m-0 text-base">{t.body}</dd>
              </div>
            );
          })}
        </dl>
      </div>
    </section>
  );
}

export function Connectors() {
  return (
    <section id="connectors" aria-labelledby="connectors-heading" className={`${GUTTER} flex flex-col gap-9 py-20 sm:py-[104px]`}>
      <SectionIntro id="connectors-heading" title="Plug into the tools you already use">
        Connect an account in a few guided steps. Susanoo checks the connection straight away and never shows your key
        again.
      </SectionIntro>
      <ul
        className="m-0 grid list-none gap-5 p-0"
        style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(190px, 100%), 1fr))" }}
      >
        {CONNECTORS.map((c) => (
          <li key={c.name} className={cn(!c.live && "opacity-70")}>
            <Stamp size="sm" tone={c.live ? "paper" : "dim"} className="h-full p-3">
              <StampFace className="flex h-full min-h-[150px] flex-col gap-2 bg-transparent p-4">
                <span
                  className={cn(
                    "self-start px-2 py-0.5 font-label text-xs tracking-[0.12em] uppercase",
                    c.live ? "bg-vermilion text-paper" : "text-ink",
                  )}
                >
                  {c.live ? "Available" : "On the roadmap"}
                </span>
                <h3 className="mk-display text-2xl">{c.name}</h3>
                <p className="text-[15px] leading-snug">{c.body}</p>
              </StampFace>
            </Stamp>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function Plans() {
  return (
    <section id="pricing" aria-labelledby="pricing-heading" className="border-t-[3px] border-ink py-16">
      <div className={`${GUTTER} flex flex-col gap-6 md:flex-row md:items-center md:justify-between`}>
        <div>
          <h2 id="pricing-heading" className="mk-display text-[clamp(26px,3vw,36px)]">
            Free, Team and Enterprise plans
          </h2>
          <p className="mt-2 text-lg">Try it free. Larger teams get private repos, custom rules, SSO and support.</p>
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
    <section id="demo" aria-labelledby="demo-heading" className="px-[clamp(20px,4vw,48px)] pt-8 pb-24 sm:pb-[104px]">
      <div className="mx-auto max-w-[1280px] border-[3px] border-ink bg-vermilion p-[clamp(24px,4vw,56px)] text-paper shadow-[12px_12px_0_var(--ink)]">
        <div className="flex flex-col gap-8 md:flex-row md:items-center md:justify-between">
          <div className="flex max-w-[620px] flex-col gap-2.5">
            <h2 id="demo-heading" className="mk-display text-[clamp(30px,3.4vw,46px)] leading-[1.05]">
              Watch a whole run, start to finish
            </h2>
            <p className="text-[19px] leading-relaxed">
              A replay of a saved run: the heads build a reviewer, catch it missing a bug, fix it and prove the fix. No
              sign-in needed.
            </p>
          </div>
          <Link
            href="/demo"
            className="inline-flex w-fit items-center rounded-[2px] bg-paper px-8 py-[18px] text-[19px] font-semibold text-ink hover:bg-mustard focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-paper"
          >
            Open full screen
          </Link>
        </div>
        <div className="mt-10 border-2 border-ink bg-ink p-2 sm:p-3">
          <DemoReplay />
        </div>
      </div>
    </section>
  );
}
