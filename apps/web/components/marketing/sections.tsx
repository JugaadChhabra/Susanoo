import Link from "next/link";
import type { CSSProperties } from "react";
import { Check } from "lucide-react";
import { cn } from "@/lib/utils";
import { DemoReplay } from "@/components/app/demo-replay";
import { CONNECTORS, PLANS, REPO_URL, TEAM_LINKS, TRUST_POINTS, USE_CASES } from "@/lib/marketing";
import { PROOF_RUN, proofStats, traceUrl } from "@/lib/proof";
import { HankoSeal, Stamp, StampFace } from "@/components/stamp";
import { SerpentStamp } from "./serpent-stamp";
import { FOCUS_RING } from "./site-chrome";

const PRIMARY = `mk-btn mk-btn-swap inline-flex items-center justify-center rounded-[2px] bg-vermilion px-7 py-4 text-lg font-semibold text-paper hover:bg-ink ${FOCUS_RING}`;
const SECONDARY = `mk-btn mk-btn-vermilion inline-flex items-center justify-center rounded-[2px] border-2 border-ink px-[26px] py-3.5 text-lg font-semibold text-ink hover:bg-paper ${FOCUS_RING}`;

/** Page gutters shared by every section. */
const GUTTER = "mx-auto w-full max-w-[1280px] px-[clamp(20px,4vw,48px)]";

/** The spec from the hero use case, word for word. [PRD › Hero use case: the code review agent] */
const EXAMPLE_SPEC =
  "Build a code review app for our Python services. Developers paste a diff or a PR link and get a review. Rules: never log PII; every FastAPI endpoint needs auth; no raw SQL string formatting; no secrets in code; flag missing error handling on external calls. Severity: block, warn, or nit.";

function StatusTag({ live, label, className }: { live: boolean; label: string; className?: string }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 self-start px-2 py-0.5 font-label text-xs tracking-[0.12em] uppercase",
        live ? "bg-vermilion text-paper" : "border border-ink text-ink",
        className,
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
          className="vert mk-hero-brush hidden self-start border-[3px] border-vermilion px-2 py-3.5 font-mincho text-[30px] leading-[1.1] font-extrabold text-vermilion sm:block"
        >
          八岐退治
        </div>
        <div className="flex min-w-0 flex-col gap-7">
          <h1
            id="hero-heading"
            className="mk-display mk-hero-rise text-[clamp(34px,4.4vw,60px)] leading-[1.02] [overflow-wrap:anywhere] sm:[overflow-wrap:normal] [--delay:80ms]"
          >
            AI agents are a many-headed mess. <span className="text-vermilion">Susanoo tames them.</span>
          </h1>
          <p className="mk-hero-rise max-w-[34em] text-[21px] leading-normal [--delay:180ms]">
            Describe the agent you need in plain English. Susanoo builds it into a working app, attacks it with test cases,
            works out why it fails, fixes the part that broke and proves the fix with traces.
          </p>
          <div className="mk-hero-rise flex flex-wrap gap-3.5 [--delay:260ms]">
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
    <section data-reveal aria-labelledby="spec-heading" className="bg-ink px-[clamp(20px,4vw,48px)] py-20 text-paper">
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
        {/* The wrapper settles and isolates, so the figure's ::after shadow sits behind the paper, not the section. */}
        <div className="mk-anim mk-telegram-form isolate min-w-0 flex-[1_1_560px]">
          <figure
            aria-labelledby="spec-heading"
            className="mk-anim mk-offset-shadow mk-telegram-shadow m-0 border-2 border-paper bg-paper text-ink [--offset-border:2px]"
          >
            <div className="mk-anim mk-telegram-bar flex items-center justify-between gap-3 bg-vermilion px-5 py-2.5 text-paper">
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
      <div data-reveal-scope className="mx-auto flex max-w-[1280px] flex-col gap-11">
        <div className="mk-anim mk-proof-shake relative">
          <SectionIntro id="proof-heading" title="Before and after a fix, from a real run">
            In a test round, the code reviewer Susanoo built let an access token typed into code slip through. Susanoo
            traced the miss to the agent&apos;s instructions, changed three lines and kept the change only after every test
            was rerun. These are the scores either side of that fix.
          </SectionIntro>
          <HankoSeal char="検証済" label="VERIFIED" rotate={10}
            className="mk-anim mk-ring mk-proof-seal absolute top-0 right-2 hidden lg:inline-flex"
          />
        </div>
        <dl data-reveal className="grid gap-8 sm:grid-cols-3">
          {stats.map((s, i) => (
            <div key={s.label} className="mk-anim mk-proof-stat mk-lift" style={{ "--i": i } as CSSProperties}>
              <Stamp className="h-full p-4">
                <StampFace className="flex h-full flex-col gap-2.5 p-6">
                  <dt className="font-label text-[13px] tracking-[0.14em] uppercase">{s.label}</dt>
                  <dd className="m-0 flex flex-wrap items-baseline gap-3">
                    <span
                      className="mk-anim mk-proof-before font-label text-xl line-through decoration-1"
                      aria-label={`before ${s.before}`}
                    >
                      {s.before}
                    </span>
                    <span aria-hidden className="mk-anim mk-proof-arrow">
                      →
                    </span>
                    <span
                      className="mk-anim mk-proof-after mk-display text-5xl text-vermilion"
                      aria-label={`after ${s.after}`}
                    >
                      {s.after}
                    </span>
                  </dd>
                  <dd className="m-0 text-base leading-snug font-medium">{s.note}</dd>
                </StampFace>
              </Stamp>
            </div>
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
      <div data-reveal className="flex flex-wrap gap-7">
        <div className="grid flex-[2_1_460px] gap-7">
          {live.map((u) => (
            <div key={u.title} className="mk-anim mk-usecase-live mk-lift">
              <Stamp className="h-full">
                <StampFace tone="vermilion" as="article" className="flex h-full flex-col gap-4 p-8">
                  <div className="flex items-start justify-between">
                    <StatusTag live label="Live now" />
                    <span aria-hidden className="mk-anim mk-usecase-kanji font-mincho text-[30px] leading-none font-extrabold">
                      査読
                    </span>
                  </div>
                  <h3 className="mk-display text-[40px] leading-tight">{u.title}</h3>
                  <p className="max-w-[30em] text-lg">{u.body}</p>
                  <Link
                    href="/projects/new"
                    className="mk-btn mt-auto inline-flex w-fit items-center rounded-[2px] bg-paper px-6 py-3.5 font-semibold text-ink hover:bg-mustard focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-mustard"
                  >
                    Start building
                  </Link>
                </StampFace>
              </Stamp>
            </div>
          ))}
        </div>
        <ul className="m-0 flex flex-[1_1_300px] list-none flex-col gap-4 p-0">
          {later.map((u, i) => (
            <li
              key={u.title}
              className="mk-anim mk-usecase-later flex flex-col gap-1 border-2 border-dashed border-ink p-5 opacity-80"
              style={{ "--i": i } as CSSProperties}
            >
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
        <dl
          data-reveal
          className="grid gap-9"
          style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(250px, 100%), 1fr))" }}
        >
          {TRUST_POINTS.map((t, i) => {
            const seal = SEALS[i % SEALS.length];
            return (
              <div
                key={t.title}
                className="mk-trust-col flex flex-col gap-3.5"
                style={{ "--i": i, "--tilt": `${seal.rotate}deg` } as CSSProperties}
              >
                {/* The seal sits in the dt so the dl holds only dt and dd. Its holder takes the hover, so the slam's
                    held end state never fights it. */}
                <dt className="flex flex-col gap-3.5">
                  <span className="mk-trust-hold flex w-fit">
                    <HankoSeal
                      char={seal.char}
                      shape={seal.shape}
                      rotate={seal.rotate}
                      className="mk-anim mk-ring mk-trust-seal relative"
                    />
                  </span>
                  <span className="mk-anim mk-trust-rise mk-display text-[22px] leading-tight">{t.title}</span>
                </dt>
                <dd className="mk-anim mk-trust-rise m-0 text-base">{t.body}</dd>
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
        data-reveal
        className="m-0 grid list-none gap-5 p-0"
        style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(190px, 100%), 1fr))" }}
      >
        {CONNECTORS.map((c, i) => (
          <li
            key={c.name}
            className={cn("mk-anim mk-connector", c.live ? "mk-lift" : "mk-connector-dim opacity-70")}
            style={{ "--i": i } as CSSProperties}
          >
            <Stamp size="sm" tone={c.live ? "paper" : "dim"} className="h-full p-3">
              <StampFace className="flex h-full min-h-[150px] flex-col gap-2 bg-transparent p-4">
                <span
                  className={cn(
                    "self-start px-2 py-0.5 font-label text-xs tracking-[0.12em] uppercase",
                    c.live ? "mk-anim mk-connector-tag bg-vermilion text-paper" : "text-ink",
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

/** Free / Team / Enterprise, inline on the landing page. Prices stay blank or "Contact us" until the cfo.ai model sets them. [PRD › Business model (inputs for cfo.ai)] */
export function Plans() {
  return (
    <section id="pricing" aria-labelledby="pricing-heading" className="border-t-[3px] border-ink">
      <div className={`${GUTTER} flex flex-col gap-10 py-20 sm:py-[104px]`}>
        <SectionIntro id="pricing-heading" title="Pick the plan that fits your team">
          Try Susanoo free. We&apos;re setting Team and Enterprise prices from the real cost of each run, so for now just
          get in touch.
        </SectionIntro>
        {/* The featured plan presses in last, so it takes the last slot in the stagger and the rest close up. */}
        <div data-reveal>
          <ul
            className="mk-anim mk-plans-shake m-0 grid list-none gap-7 p-0 lg:grid-cols-3"
            style={{ "--last": PLANS.length - 1 } as CSSProperties}
          >
            {PLANS.map((plan) => {
              const { featured } = plan;
              const order = featured ? PLANS.length - 1 : PLANS.filter((p) => !p.featured).indexOf(plan);
              return (
                <li
                  key={plan.name}
                  className={cn("mk-anim mk-plan mk-lift", featured && "mk-plan-featured mk-lift-mustard")}
                  style={{ "--i": order } as CSSProperties}
                >
                  <Stamp tone={featured ? "mustard" : "paper"} className="h-full">
                    <StampFace tone={featured ? "ink" : "light"} as="article" className="flex h-full flex-col gap-4 p-7">
                      <div className="flex items-start justify-between gap-3">
                        <h3 className="mk-display text-[32px] leading-tight">{plan.name}</h3>
                        {featured && <StatusTag live label="Most teams" className="mk-anim mk-plan-badge" />}
                      </div>
                      <p className="text-base lg:min-h-12">{plan.who}</p>
                      {/* Blank until cfo.ai sets prices; on wide screens it keeps its height so the feature lists line up. */}
                      <p
                        className={cn(
                          "mk-display text-3xl lg:min-h-9",
                          featured ? "text-mustard" : "text-vermilion",
                          !plan.price && "hidden lg:block",
                        )}
                      >
                        {plan.price}
                      </p>
                      <ul className="m-0 flex flex-1 list-none flex-col gap-3 p-0 text-base">
                        {plan.features.map((feature, i) => (
                          <li key={feature} className="mk-anim mk-plan-feature flex gap-3" style={{ "--i": i } as CSSProperties}>
                            <Check
                              aria-hidden
                              className={cn("mt-1 size-4 shrink-0", featured ? "text-mustard" : "text-vermilion")}
                            />
                            {feature}
                          </li>
                        ))}
                      </ul>
                      <Link
                        href={plan.cta.href}
                        className={cn(
                          "mt-4 inline-flex items-center justify-center rounded-[2px] px-6 py-3.5 text-lg font-semibold",
                          featured
                            ? "bg-paper text-ink hover:bg-mustard focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-mustard"
                            : `bg-ink text-paper hover:bg-vermilion ${FOCUS_RING}`,
                        )}
                      >
                        {plan.cta.label}
                        {plan.price && <span className="sr-only"> about the {plan.name} plan</span>}
                      </Link>
                    </StampFace>
                  </Stamp>
                </li>
              );
            })}
          </ul>
        </div>
        <div
          id="contact"
          className="flex scroll-mt-6 flex-col gap-6 border-2 border-dashed border-ink p-6 sm:p-8 md:flex-row md:items-center md:justify-between"
        >
          <div className="max-w-xl">
            <h3 className="mk-display text-[clamp(24px,2.6vw,32px)]">Talk to us</h3>
            <p className="mt-2 text-lg">
              Tell us what your team wants to automate and we&apos;ll set up a plan with you. You can reach Muaaz and
              Jugaad through their sites.
            </p>
          </div>
          <ul className="m-0 flex list-none flex-col gap-3 p-0 sm:flex-row">
            {TEAM_LINKS.map((site) => (
              <li key={site.href}>
                <a href={site.href} target="_blank" rel="noopener noreferrer" className={SECONDARY}>
                  {site.label}
                </a>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </section>
  );
}

export function DemoBand() {
  return (
    <section data-reveal id="demo" aria-labelledby="demo-heading" className="px-[clamp(20px,4vw,48px)] pt-8 pb-24 sm:pb-[104px]">
      {/* The wrapper settles and isolates, so the block's ::after ink shadow sits behind it, not behind the section. */}
      <div className="mk-anim mk-demo-block isolate mx-auto max-w-[1280px]">
        <div className="mk-anim mk-offset-shadow mk-demo-shadow border-[3px] border-ink bg-vermilion p-[clamp(24px,4vw,56px)] text-paper [--offset-border:3px]">
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
              className="mk-anim mk-demo-open mk-btn inline-flex w-fit items-center rounded-[2px] bg-paper px-8 py-[18px] text-[19px] font-semibold text-ink hover:bg-mustard focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-paper"
            >
              Open full screen
            </Link>
          </div>
          <div className="stamp-palette mt-10 border-2 border-ink bg-ink p-2 sm:p-3">
            <DemoReplay />
          </div>
        </div>
      </div>
    </section>
  );
}
