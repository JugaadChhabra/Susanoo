import { expect, test, type Locator } from "@playwright/test";

/** Opacity as painted: the element's own opacity times every ancestor's, so a hidden wrapper counts too. */
function paintedOpacity(locator: Locator): Promise<number> {
  return locator.evaluate((el) => {
    let opacity = 1;
    for (let node: Element | null = el; node; node = node.parentElement) {
      opacity *= Number(getComputedStyle(node).opacity);
    }
    return opacity;
  });
}

/** The ::after layer's computed opacity. */
function afterOpacity(locator: Locator): Promise<number> {
  return locator.evaluate((el) => Number(getComputedStyle(el, "::after").opacity));
}

/**
 * The landing page never hides content behind JavaScript or motion: with reduced motion or with JS off, the hero, the
 * spec form, the last How it works head and the Trust section are all there at full opacity. [docs/motion-spec.md §4, §5]
 */
for (const [name, options] of [
  ["reduced motion", { reducedMotion: "reduce" }],
  ["JavaScript off", { javaScriptEnabled: false }],
] as const) {
  test.describe(`landing page with ${name}`, () => {
    test.use(options);

    test("the hero, the spec form, How it works and Trust are visible at full opacity", async ({ page }) => {
      await page.goto("/");
      const targets = [
        page.getByRole("heading", { level: 1, name: /many-headed mess/ }),
        page.getByRole("figure", { name: "You write a spec like this one" }),
        page.getByRole("region", { name: "Eight heads, one job each" }).getByRole("listitem").last(),
        page.getByRole("region", { name: "Built so it can't fool you" }),
        page.locator("#proof dl > div").last(),
        page.locator("#proof dl .text-vermilion").last(),
        page.locator("#proof .mk-proof-seal"),
        page.locator("#use-cases .mk-usecase-live"),
        page.locator("#use-cases .mk-usecase-kanji"),
        page.locator("#trust .mk-trust-seal").last(),
        page.locator("#trust dd").last(),
      ];
      for (const target of targets) {
        await target.scrollIntoViewIfNeeded();
        await expect(target).toBeVisible();
        await expect.poll(() => paintedOpacity(target)).toBe(1);
      }
      // The VERIFIED seal's ink ring only ever shows mid-bleed, so at rest it is invisible.
      await expect.poll(() => afterOpacity(page.locator("#proof .mk-proof-seal"))).toBe(0);
      await expect.poll(() => afterOpacity(page.locator("#trust .mk-trust-seal").first())).toBe(0);
      // The coming-soon cards are dimmed on purpose, so they sit at their own 80%.
      await expect.poll(() => paintedOpacity(page.locator("#use-cases li").last())).toBe(0.8);
      if (name === "reduced motion") {
        // Reduced motion means motion is never switched on, so nothing waits for the observer.
        await expect(page.locator("html")).not.toHaveAttribute("data-motion", "on");
      }
    });
  });
}

test("with motion on, the spec form reveals once it scrolls into view", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.goto("/");
  await expect(page.locator("html")).toHaveAttribute("data-motion", "on");
  const form = page.getByRole("figure", { name: "You write a spec like this one" });
  const section = page.locator("section[data-reveal]").filter({ has: form });
  // Below the fold it waits, unrevealed, at its first frame.
  await expect(section).not.toHaveAttribute("data-inview");
  await expect.poll(() => paintedOpacity(form)).toBe(0);
  await form.scrollIntoViewIfNeeded();
  await expect(section).toHaveAttribute("data-inview", "");
  await expect.poll(() => paintedOpacity(form)).toBe(1);
});

test("with motion on, How it works settles in and the lit head thuds and lifts until paused", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.goto("/");
  await expect(page.locator("html")).toHaveAttribute("data-motion", "on");
  const section = page.locator("section#how-it-works");
  const heads = section.locator("li");
  await expect(heads).toHaveCount(8);
  // Below the fold every head waits, unrevealed, at its first frame.
  await expect(section).not.toHaveAttribute("data-inview");
  await expect.poll(() => paintedOpacity(heads.last())).toBe(0);
  await section.getByRole("heading", { level: 2 }).scrollIntoViewIfNeeded();
  await expect(section).toHaveAttribute("data-inview", "");
  await expect.poll(() => paintedOpacity(heads.last())).toBe(1);

  // The lit head's stamp presses in and sits 4px up.
  const litStamp = section.locator('li[aria-current="step"] > .perf');
  await expect(section).toHaveAttribute("data-sequence", "playing");
  await expect(litStamp).toHaveCSS("translate", "0px -4px");
  await expect(litStamp).toHaveCSS("animation-name", "mk-press");

  // Paused: the head stays lit but nothing thuds or lifts.
  await section.getByRole("button", { name: "Pause the sequence" }).click();
  await expect(section).toHaveAttribute("data-sequence", "still");
  await expect(litStamp).toHaveCSS("animation-name", "none");
  await expect(litStamp).toHaveCSS("translate", "none");
});

test("with motion on, Proof's stats press in once the stats are in view, then VERIFIED slams with one ring and one shake", async ({
  page,
}) => {
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.goto("/");
  await expect(page.locator("html")).toHaveAttribute("data-motion", "on");
  const section = page.locator("section#proof");
  const stats = section.locator("dl[data-reveal]");
  const cards = stats.locator("> div");
  const seal = section.locator(".mk-proof-seal");
  const intro = section.locator(".mk-proof-shake");
  await expect(cards).toHaveCount(3);

  // Below the fold the stats and the seal wait, unrevealed, at their first frame; the ring is never drawn at rest.
  await expect(stats).not.toHaveAttribute("data-inview");
  await expect.poll(() => paintedOpacity(cards.last())).toBe(0);
  await expect.poll(() => paintedOpacity(seal)).toBe(0);
  await expect.poll(() => afterOpacity(seal)).toBe(0);
  // The seal and the intro sit outside the stats, but their reveal scope keeps them waiting on the stats.
  await expect(seal).toHaveCSS("animation-play-state", "paused");
  await expect(intro).toHaveCSS("animation-play-state", "paused");

  await stats.scrollIntoViewIfNeeded();
  await expect(stats).toHaveAttribute("data-inview", "");
  await expect(cards.first()).toHaveCSS("animation-name", "mk-press");
  await expect(cards.last().locator(".text-vermilion")).toHaveCSS("animation-name", "mk-slam");
  await expect(seal).toHaveCSS("animation-name", "mk-slam");
  await expect(intro).toHaveCSS("animation-name", "mk-shake");
  await expect.poll(() => seal.evaluate((el) => getComputedStyle(el, "::after").animationName)).toBe("mk-bleed");

  // Everything lands: full opacity, the seal back at its own 10deg tilt, and the ring bled away.
  await expect.poll(() => paintedOpacity(cards.last())).toBe(1);
  await expect.poll(() => paintedOpacity(seal)).toBe(1);
  await expect.poll(() => seal.evaluate((el) => getComputedStyle(el).rotate)).toMatch(/^(none|0deg)$/);
  await expect(seal).toHaveCSS("transform", /^matrix\(0\.98/);
  await expect.poll(() => afterOpacity(seal)).toBe(0);

  // Once pressed in, a card still lifts on hover: the press holds nothing that fights the lift.
  await cards.first().hover();
  await expect(cards.first().locator("> .perf")).toHaveCSS("translate", "-3px -4px");

  // The stats still read the same to a screen reader.
  await expect(cards.first().getByLabel(/^before /)).toHaveCount(1);
  await expect(cards.first().getByLabel(/^after /)).toHaveCount(1);
});

test("with motion on, UseCases' live card presses in, its kanji slams and the coming-soon cards settle to 80%", async ({
  page,
}) => {
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.goto("/");
  await expect(page.locator("html")).toHaveAttribute("data-motion", "on");
  const reveal = page.locator("#use-cases [data-reveal]");
  const live = reveal.locator(".mk-usecase-live");
  const kanji = reveal.locator(".mk-usecase-kanji");
  const later = reveal.locator("li");

  // Below the fold everything waits, unrevealed, at its first frame.
  await expect(reveal).not.toHaveAttribute("data-inview");
  await expect.poll(() => paintedOpacity(live)).toBe(0);
  await expect.poll(() => paintedOpacity(later.last())).toBe(0);

  await reveal.scrollIntoViewIfNeeded();
  await expect(reveal).toHaveAttribute("data-inview", "");
  await expect(live).toHaveCSS("animation-name", "mk-press");
  await expect(live).toHaveCSS("animation-duration", "0.46s");
  await expect(kanji).toHaveCSS("animation-name", "mk-slam");
  await expect(kanji).toHaveCSS("animation-delay", "0.3s");
  await expect(later.first()).toHaveCSS("animation-name", "mk-settle");
  await expect(later.first()).toHaveCSS("animation-delay", "0.2s");
  await expect(later.nth(1)).toHaveCSS("animation-delay", "0.28s");

  // Everything lands; the coming-soon cards end at their own opacity-80.
  await expect.poll(() => paintedOpacity(kanji)).toBe(1);
  await expect.poll(() => paintedOpacity(later.last())).toBe(0.8);
});

test("with motion on, the Trust seals slam in order on their own tilt, ring once, and straighten on hover", async ({
  page,
}) => {
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.goto("/");
  await expect(page.locator("html")).toHaveAttribute("data-motion", "on");
  const dl = page.locator("#trust dl[data-reveal]");
  const columns = dl.locator("> div");
  const seals = dl.locator(".mk-trust-seal");
  await expect(seals).toHaveCount(4);

  // Below the fold the seals and their text wait, unrevealed; the rings are never drawn at rest.
  await expect(dl).not.toHaveAttribute("data-inview");
  await expect.poll(() => paintedOpacity(seals.last())).toBe(0);
  await expect.poll(() => paintedOpacity(dl.locator("dd").last())).toBe(0);
  await expect.poll(() => afterOpacity(seals.first())).toBe(0);

  await dl.scrollIntoViewIfNeeded();
  await expect(dl).toHaveAttribute("data-inview", "");
  // In order, 140ms apart, with the label and description 120ms behind their seal.
  for (const [i, delay] of ["0s", "0.14s", "0.28s", "0.42s"].entries()) {
    await expect(seals.nth(i)).toHaveCSS("animation-name", "mk-slam");
    await expect(seals.nth(i)).toHaveCSS("animation-delay", delay);
  }
  await expect(columns.nth(2).locator("dt")).toHaveCSS("animation-name", "mk-rise");
  await expect(columns.nth(2).locator("dt")).toHaveCSS("animation-delay", "0.4s");
  await expect(columns.nth(2).locator("dd")).toHaveCSS("animation-delay", "0.4s");
  await expect.poll(() => seals.first().evaluate((el) => getComputedStyle(el, "::after").animationName)).toBe("mk-bleed");
  // The ring takes the seal's shape: round on the round seal, rounded corners on the rest.
  const radii = await seals.evaluateAll((els) =>
    els.map((el) => [getComputedStyle(el).borderRadius, getComputedStyle(el, "::after").borderRadius]),
  );
  for (const [seal, ring] of radii) expect(ring).toBe(seal);
  expect(radii[1][0]).not.toBe(radii[0][0]);

  // Everything lands, each seal on its own tilt (-4, 3, -2, 5), and the rings bleed away.
  await expect.poll(() => paintedOpacity(seals.last())).toBe(1);
  await expect.poll(() => paintedOpacity(dl.locator("dd").last())).toBe(1);
  await expect.poll(() => seals.last().evaluate((el) => getComputedStyle(el).rotate)).toMatch(/^(none|0deg)$/);
  for (const [i, tilt] of [-4, 3, -2, 5].entries()) {
    expect(await seals.nth(i).evaluate((el) => el.style.transform)).toBe(`rotate(${tilt}deg)`);
  }
  await expect.poll(() => afterOpacity(seals.last())).toBe(0);

  // Hovering a column picks its seal up and straightens it.
  const hold = columns.first().locator(".mk-trust-hold");
  await expect(hold).toHaveCSS("transition-duration", "0.16s, 0.16s");
  await columns.first().locator("dd").hover();
  await expect(hold).toHaveCSS("rotate", "4deg");
  await expect(hold).toHaveCSS("scale", "1.06");
});
