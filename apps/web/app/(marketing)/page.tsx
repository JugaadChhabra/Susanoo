import type { Metadata } from "next";
import { HowItWorks } from "@/components/marketing/how-it-works";
import { Connectors, DemoBand, Hero, Plans, Trust, UseCases } from "@/components/marketing/sections";
import { MarketingShell } from "@/components/marketing/site-chrome";
import { SITE_DESCRIPTION, SITE_TITLE } from "@/lib/marketing";

export const metadata: Metadata = {
  title: { absolute: SITE_TITLE },
  description: SITE_DESCRIPTION,
  alternates: { canonical: "/" },
};

/** Landing page, sections in PRD order. [PRD › Frontend (marketing site + app) and API › Surface 1: Marketing site] */
export default function Page() {
  return (
    <MarketingShell>
      <Hero />
      <HowItWorks />
      {/* Proof (#19): before/after held-out F1, recall and false alarms from the saved run, linked to neatlogs traces. */}
      <UseCases />
      <Trust />
      <Connectors />
      <Plans />
      <DemoBand />
    </MarketingShell>
  );
}
