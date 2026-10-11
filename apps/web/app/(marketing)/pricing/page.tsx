import type { Metadata } from "next";
import Link from "next/link";
import { Check } from "lucide-react";
import { cn } from "@/lib/utils";
import { FOCUS_RING, MarketingShell } from "@/components/marketing/site-chrome";
import { PLANS, TEAM_LINKS } from "@/lib/marketing";

export const metadata: Metadata = {
  title: "Pricing · Susanoo",
  description: "Free, Team and Enterprise plans for Susanoo. Try it free; talk to us about Team and Enterprise.",
  alternates: { canonical: "/pricing" },
};

/** Free / Team / Enterprise. Prices stay blank or "Contact us" until the cfo.ai model sets them. [PRD › Business model (inputs for cfo.ai)] */
export default function Page() {
  return (
    <MarketingShell>
      <section aria-labelledby="pricing-heading" className="mx-auto w-full max-w-6xl px-4 pt-16 pb-20 sm:px-6 sm:pt-24">
        <div className="max-w-2xl">
          <h1 id="pricing-heading" className="mk-display text-[2.4rem] leading-[1.05] sm:text-6xl">
            Pick the plan that fits your team
          </h1>
          <p className="mt-6 text-lg leading-relaxed text-[var(--mk-storm)]">
            Try Susanoo free. We&apos;re setting Team and Enterprise prices from the real cost of each run, so for now
            just get in touch.
          </p>
        </div>

        <ul className="mt-12 grid gap-6 lg:grid-cols-3">
          {PLANS.map((plan) => {
            const featured = plan.name === "Team";
            return (
              <li
                key={plan.name}
                className={cn(
                  "flex flex-col rounded-2xl border p-6 sm:p-8",
                  featured ? "border-[var(--mk-ink)] bg-[var(--mk-ink)] text-white" : "border-[var(--mk-rule)] bg-white",
                )}
              >
                <h2 className="mk-display text-2xl">{plan.name}</h2>
                <p className={cn("mt-1 text-sm lg:min-h-10", featured ? "text-slate-300" : "text-[var(--mk-storm)]")}>{plan.who}</p>
                {/* Blank until cfo.ai sets prices; on wide screens it keeps its height so the feature lists line up. */}
                <p className={cn("mk-display mt-6 text-3xl lg:min-h-9", !plan.price && "hidden lg:block")}>{plan.price}</p>
                <ul className="mt-6 flex-1 space-y-3 text-sm">
                  {plan.features.map((feature) => (
                    <li key={feature} className="flex gap-3">
                      <Check
                        aria-hidden
                        className={cn("mt-0.5 size-4 shrink-0", featured ? "text-[var(--mk-tide-soft)]" : "text-[var(--mk-tide)]")}
                      />
                      {feature}
                    </li>
                  ))}
                </ul>
                <Link
                  href={plan.cta.href}
                  className={cn(
                    "mt-8 inline-flex h-12 items-center justify-center rounded-full px-6 font-medium",
                    featured
                      ? "bg-white text-[var(--mk-ink)] hover:bg-[var(--mk-mist)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--mk-tide-soft)]"
                      : `bg-[var(--mk-ink)] text-white hover:bg-[var(--mk-storm)] ${FOCUS_RING}`,
                  )}
                >
                  {plan.cta.label}
                  <span className="sr-only"> about the {plan.name} plan</span>
                </Link>
              </li>
            );
          })}
        </ul>
      </section>

      <section
        id="contact"
        aria-labelledby="contact-heading"
        className="scroll-mt-6 border-t border-[var(--mk-rule)] py-16"
      >
        <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 px-4 sm:px-6 md:flex-row md:items-center md:justify-between">
          <div className="max-w-xl">
            <h2 id="contact-heading" className="mk-display text-2xl sm:text-3xl">
              Talk to us
            </h2>
            <p className="mt-2 text-[var(--mk-storm)]">
              Tell us what your team wants to automate and we&apos;ll set up a plan with you.
            </p>
          </div>
          <ul className="flex flex-col gap-3 sm:flex-row">
            {TEAM_LINKS.map((site) => (
              <li key={site.href}>
                <a
                  href={site.href}
                  target="_blank"
                  rel="noopener noreferrer"
                  className={`inline-flex h-12 items-center justify-center rounded-full border border-[var(--mk-ink)] px-6 font-medium text-[var(--mk-ink)] hover:bg-white ${FOCUS_RING}`}
                >
                  {site.label}
                </a>
              </li>
            ))}
          </ul>
        </div>
      </section>
    </MarketingShell>
  );
}
