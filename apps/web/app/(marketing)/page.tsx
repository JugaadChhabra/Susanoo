import type { Metadata } from "next";
import { Bricolage_Grotesque } from "next/font/google";
import { HowItWorks } from "@/components/marketing/how-it-works";
import { Connectors, DemoBand, Hero, Plans, Trust, UseCases } from "@/components/marketing/sections";
import { SiteFooter, SiteHeader } from "@/components/marketing/site-chrome";
import { SITE_DESCRIPTION, SITE_TITLE } from "@/lib/marketing";

const display = Bricolage_Grotesque({ subsets: ["latin"], variable: "--font-display" });

export const metadata: Metadata = {
  title: { absolute: SITE_TITLE },
  description: SITE_DESCRIPTION,
  alternates: { canonical: "/" },
};

/** Landing page, sections in PRD order. [PRD › Frontend (marketing site + app) and API › Surface 1: Marketing site] */
export default function Page() {
  return (
    <div className={`mk ${display.variable} flex min-h-full flex-1 flex-col`}>
      <a
        href="#main"
        className="sr-only z-50 rounded-md bg-white px-4 py-2 focus:not-sr-only focus:fixed focus:top-3 focus:left-3"
      >
        Skip to content
      </a>
      <SiteHeader />
      <main id="main" className="flex-1">
        <Hero />
        <HowItWorks />
        {/* Proof (#19): before/after held-out F1, recall and false alarms from the saved run, linked to neatlogs traces. */}
        <UseCases />
        <Trust />
        <Connectors />
        <Plans />
        <DemoBand />
      </main>
      <SiteFooter />
    </div>
  );
}
