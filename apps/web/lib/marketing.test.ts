import { describe, expect, it } from "vitest";
import { HEADS } from "./run-state";
import { CONNECTORS, FOOTER_LINKS, HEAD_STEPS, PLANS, USE_CASES, nextHeadIndex, siteUrl } from "./marketing";

describe("HEAD_STEPS", () => {
  it("lists the eight heads in graph order, grouped into the PRD's phases in order", () => {
    expect(HEAD_STEPS.map((s) => s.id)).toEqual(HEADS.map((h) => h.id));
    const phases = HEAD_STEPS.map((s) => s.phase).filter((p, i, all) => all.indexOf(p) === i);
    expect(phases).toEqual(["Intake", "Build", "Simulate", "Diagnose", "Fix", "Verify"]);
  });
});

describe("nextHeadIndex", () => {
  it("walks the heads in order and starts again after the Verifier", () => {
    expect(nextHeadIndex(0)).toBe(1);
    expect(nextHeadIndex(HEAD_STEPS.length - 1)).toBe(0);
  });
});

describe("marketing content", () => {
  it("marks code review as the only live use case", () => {
    expect(USE_CASES.filter((u) => u.live).map((u) => u.title)).toEqual(["Code review"]);
    expect(USE_CASES.map((u) => u.title)).toEqual(["Code review", "Support", "Operations", "Compliance"]);
  });
  it("marks GitHub and Anthropic live and everything else roadmap", () => {
    expect(CONNECTORS.filter((c) => c.live).map((c) => c.name)).toEqual(["GitHub", "Anthropic"]);
    expect(CONNECTORS.filter((c) => !c.live).map((c) => c.name)).toEqual(["Slack", "Jira", "Linear", "Postgres"]);
  });
  it("links the repo, #neatHack and both team sites in the footer", () => {
    const hrefs = FOOTER_LINKS.map((l) => l.href);
    expect(hrefs).toEqual([
      "https://github.com/JugaadChhabra/Susanoo",
      "https://x.com/hashtag/neatHack",
      "https://muaazshaikh.com",
      "https://jugaadchhabra.dev",
    ]);
  });
});

describe("siteUrl", () => {
  it("prefers the configured site URL", () => {
    expect(siteUrl({ NEXT_PUBLIC_SITE_URL: "https://susanoo.example/", VERCEL_PROJECT_PRODUCTION_URL: "x.vercel.app" }).href).toBe(
      "https://susanoo.example/",
    );
  });
  it("falls back to Vercel's production domain, then to localhost", () => {
    expect(siteUrl({ VERCEL_PROJECT_PRODUCTION_URL: "susanoo.vercel.app" }).href).toBe("https://susanoo.vercel.app/");
    expect(siteUrl({}).href).toBe("http://localhost:3000/");
  });
  it("accepts a bare domain and never throws on a bad value", () => {
    expect(siteUrl({ NEXT_PUBLIC_SITE_URL: "susanoo.app" }).href).toBe("https://susanoo.app/");
    expect(siteUrl({ NEXT_PUBLIC_SITE_URL: "http://", VERCEL_PROJECT_PRODUCTION_URL: "s.vercel.app" }).href).toBe(
      "https://s.vercel.app/",
    );
  });
});

describe("PLANS", () => {
  it("lists Free, Team and Enterprise with the PRD's features", () => {
    expect(PLANS.map((p) => p.name)).toEqual(["Free", "Team", "Enterprise"]);
    expect(PLANS.map((p) => p.features.length)).toEqual([2, 3, 4]);
    expect(PLANS[2].features.join(" ")).toMatch(/SSO.*self-hosted.*custom domains.*support/i);
  });
  it("shows no prices until the cost model sets them: paid plans say Contact us", () => {
    expect(PLANS.map((p) => p.price)).toEqual([null, "Contact us", "Contact us"]);
  });
});
