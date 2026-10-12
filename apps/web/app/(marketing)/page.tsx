import type { Metadata } from "next";
import { HowItWorks } from "@/components/marketing/how-it-works";
import { Connectors, DemoBand, Hero, Plans, Proof, Telegram, Trust, UseCases } from "@/components/marketing/sections";
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
      <Telegram />
      <HowItWorks />
      <Proof />
      <UseCases />
      <Trust />
      <Connectors />
      <Plans />
      <DemoBand />
    </MarketingShell>
  );
}
