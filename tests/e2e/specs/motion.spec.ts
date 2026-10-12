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

/** The Telegram spec, as a reader sees it. */
const EXAMPLE_SPEC_START = /^Build a code review app for our Python services\. Developers paste .* Severity: block, warn, or nit\.$/;

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
        page.locator("#connectors li").first(),
        page.locator("#connectors li").first().getByText("Available"),
        page.locator("#pricing .mk-plan-featured"),
        page.locator("#pricing .mk-plan-badge"),
        page.locator("#pricing .mk-plan-feature").last(),
        page.getByRole("link", { name: "Open full screen" }),
      ];
      for (const target of targets) {
        await target.scrollIntoViewIfNeeded();
        await expect(target).toBeVisible();
        await expect.poll(() => paintedOpacity(target)).toBe(1);
      }
      // The VERIFIED seal's ink ring only ever shows mid-bleed, so at rest it is invisible.
      await expect.poll(() => afterOpacity(page.locator("#proof .mk-proof-seal"))).toBe(0);
      await expect.poll(() => afterOpacity(page.locator("#trust .mk-trust-seal").first())).toBe(0);
      // The demo band's ink shadow is drawn at its full 12px.
      await expect(page.locator("#demo .mk-demo-shadow")).toBeVisible();
      expect(await page.locator("#demo .mk-demo-shadow").evaluate((el) => getComputedStyle(el, "::after").translate)).toBe(
        "12px 12px",
      );
      // The roadmap connectors are dimmed on purpose, so they sit at their own 70%.
      await expect.poll(() => paintedOpacity(page.locator("#connectors li").last())).toBe(0.7);
      // The coming-soon cards are dimmed on purpose, so they sit at their own 80%.
      await expect.poll(() => paintedOpacity(page.locator("#use-cases li").last())).toBe(0.8);
      // The serpent is whole at once: the sun in place, every neck fully drawn, every head and eye showing.
      const serpent = page.getByRole("img", { name: /eight-headed serpent/ });
      // With JS off the draw still plays on load, so wait for it to land.
      const sun = serpent.locator(".mk-serpent-sun");
      await expect.poll(() => sun.evaluate((el) => getComputedStyle(el).translate)).toMatch(/^(none|0px)$/);
      const necks = serpent.locator(".mk-serpent-neck");
      await expect(necks).toHaveCount(8);
      await expect
        .poll(() => necks.evaluateAll((els) => els.map((el) => getComputedStyle(el).strokeDashoffset)))
        .toEqual(Array(8).fill("0px"));
      const heads = serpent.locator(".mk-serpent-head");
      await expect(heads).toHaveCount(8);
      await expect
        .poll(() => heads.evaluateAll((els) => els.map((el) => getComputedStyle(el).opacity)))
        .toEqual(Array(8).fill("1"));
      // The spec text is all there, in order, as real text, and the caret never shows.
      const spec = page.getByRole("figure", { name: "You write a spec like this one" }).locator("blockquote");
      await expect(spec).toHaveText(EXAMPLE_SPEC_START, { useInnerText: true });
      await expect.poll(() => paintedOpacity(spec.locator(".mk-spec-line").last())).toBe(1);
      await expect(spec.locator(".mk-spec-caret")).toBeHidden();
      // The Proof numbers are their final values, with the labels unchanged.
      const after = page.locator("#proof").getByLabel("after 100%");
      await expect(after.locator("[data-count]")).toHaveText("100%");
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
  // Each column is one dt/dd pair, the seal inside the dt.
  for (const tags of await columns.evaluateAll((els) => els.map((el) => [...el.children].map((c) => c.tagName)))) {
    expect(tags).toEqual(["DT", "DD"]);
  }
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
  await expect(columns.nth(2).locator("dt .mk-trust-rise")).toHaveCSS("animation-name", "mk-rise");
  await expect(columns.nth(2).locator("dt .mk-trust-rise")).toHaveCSS("animation-delay", "0.4s");
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

test("with motion on, the connectors settle in on a capped stagger, Available tags press after their card and roadmap cards only fade", async ({
  page,
}) => {
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.goto("/");
  await expect(page.locator("html")).toHaveAttribute("data-motion", "on");
  const ul = page.locator("#connectors ul[data-reveal]");
  const cards = ul.locator("> li");
  const live = ul.locator("> li.mk-lift");
  const dim = ul.locator("> li.mk-connector-dim");
  await expect(cards).toHaveCount(6);

  // Below the fold every card waits, unrevealed, at its first frame.
  await expect(ul).not.toHaveAttribute("data-inview");
  await expect.poll(() => paintedOpacity(cards.last())).toBe(0);

  await ul.scrollIntoViewIfNeeded();
  await expect(ul).toHaveAttribute("data-inview", "");
  // 50ms apart, stopping at 350ms however many connectors there are.
  await expect(cards.nth(1)).toHaveCSS("animation-delay", "0.05s");
  await expect(cards.last()).toHaveCSS("animation-delay", "0.25s");
  // A twelfth connector would still start by 350ms.
  expect(
    await cards.last().evaluate((el) => {
      const probe = el.cloneNode() as HTMLElement;
      probe.style.setProperty("--i", "11");
      el.parentElement!.append(probe);
      const delay = getComputedStyle(probe).animationDelay;
      probe.remove();
      return delay;
    }),
  ).toBe("0.35s");
  await expect(live.first()).toHaveCSS("animation-name", "mk-settle");
  await expect(live.first().getByText("Available")).toHaveCSS("animation-name", "mk-press");
  await expect(live.nth(1).getByText("Available")).toHaveCSS("animation-delay", "0.31s");
  // Roadmap cards fade only, and never lift on hover.
  await expect(dim.first()).toHaveCSS("animation-name", "mk-fade");
  await expect(dim.first()).not.toHaveClass(/mk-lift/);

  // Everything lands; the roadmap cards end at their own opacity-70.
  await expect.poll(() => paintedOpacity(live.first())).toBe(1);
  await expect.poll(() => paintedOpacity(cards.last())).toBe(0.7);
  await dim.first().hover();
  await expect(dim.first().locator("> .perf")).toHaveCSS("translate", "none");
  await live.first().hover();
  await expect(live.first().locator("> .perf")).toHaveCSS("translate", "-3px -4px");
});

test("with motion on, the pricing cards press in with the featured plan last, the grid shakes and its badge slams", async ({
  page,
}) => {
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.goto("/");
  await expect(page.locator("html")).toHaveAttribute("data-motion", "on");
  const reveal = page.locator("#pricing [data-reveal]");
  const grid = reveal.locator("ul.mk-plans-shake");
  const cards = grid.locator("> li");
  const featured = grid.locator("> li.mk-plan-featured");
  const badge = featured.locator(".mk-plan-badge");
  await expect(cards).toHaveCount(3);

  // Below the fold every card waits, unrevealed, at its first frame.
  await expect(reveal).not.toHaveAttribute("data-inview");
  await expect.poll(() => paintedOpacity(featured)).toBe(0);
  await expect(grid).toHaveCSS("animation-play-state", "paused");

  await reveal.scrollIntoViewIfNeeded();
  await expect(reveal).toHaveAttribute("data-inview", "");
  // 110ms apart, the featured plan last whatever its place in the grid.
  const delays = await cards.evaluateAll((els) => els.map((el) => getComputedStyle(el).animationDelay));
  expect([...delays].sort()).toEqual(["0.11s", "0.22s", "0s"].sort());
  await expect(featured).toHaveCSS("animation-delay", "0.22s");
  await expect(featured).toHaveCSS("animation-name", "mk-press");
  // The grid shakes on the featured card's impact (55% of the 460ms press), and the badge slams 200ms after.
  await expect(grid).toHaveCSS("animation-name", "mk-shake");
  await expect(grid).toHaveCSS("animation-delay", "0.473s");
  await expect(badge).toHaveCSS("animation-name", "mk-slam");
  await expect(badge).toHaveCSS("animation-delay", "0.673s");
  // Features fade only, 40ms apart; the price text never animates.
  const feature = featured.locator(".mk-plan-feature").nth(1);
  await expect(feature).toHaveCSS("animation-name", "mk-fade");
  await expect(feature).toHaveCSS("animation-delay", "0.513s");
  await expect(featured.getByText("Contact us")).toHaveCSS("animation-name", "none");

  // Everything lands.
  await expect.poll(() => paintedOpacity(badge)).toBe(1);
  await expect.poll(() => paintedOpacity(cards.last().locator(".mk-plan-feature").last())).toBe(1);

  // The featured card's hover shadow is mustard; the others' is ink.
  await featured.locator("h3").hover();
  await expect.poll(() => featured.evaluate((el) => getComputedStyle(el).filter)).toContain("6px 6px 0px");
  const mustard = await page.evaluate(() => {
    const probe = document.createElement("span");
    probe.style.color = "var(--mustard)";
    document.querySelector(".mk")!.append(probe);
    const color = getComputedStyle(probe).color;
    probe.remove();
    return color;
  });
  await expect.poll(() => featured.evaluate((el) => getComputedStyle(el).filter)).toContain(mustard);
});

test("with motion on, the demo band settles in, its ink shadow grows and Open full screen presses in at 400ms", async ({
  page,
}) => {
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.goto("/");
  await expect(page.locator("html")).toHaveAttribute("data-motion", "on");
  const section = page.locator("section#demo");
  const block = section.locator(".mk-demo-block");
  const shadow = section.locator(".mk-demo-shadow");
  const open = section.getByRole("link", { name: "Open full screen" });

  // Below the fold the block waits, unrevealed, with its shadow still tucked under it.
  await expect(section).not.toHaveAttribute("data-inview");
  await expect.poll(() => paintedOpacity(block)).toBe(0);

  await section.getByRole("heading", { level: 2 }).scrollIntoViewIfNeeded();
  await expect(section).toHaveAttribute("data-inview", "");
  await expect(block).toHaveCSS("animation-name", "mk-settle");
  await expect.poll(() => shadow.evaluate((el) => getComputedStyle(el, "::after").animationName)).toBe("mk-offset-grow");
  await expect(open).toHaveCSS("animation-name", "mk-press");
  await expect(open).toHaveCSS("animation-delay", "0.4s");

  // It lands with the full 12px ink shadow, and the button still lifts on hover.
  await expect.poll(() => paintedOpacity(open)).toBe(1);
  await expect.poll(() => shadow.evaluate((el) => getComputedStyle(el, "::after").translate)).toBe("12px 12px");
  await open.hover();
  await expect(open).toHaveCSS("translate", "-2px -2px");
});

test("header nav links draw an underline from the left on hover", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.goto("/");
  const link = page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Pricing" });
  const underline = () => link.evaluate((el) => getComputedStyle(el, "::after").scale);
  await expect.poll(underline).toBe("0 1");
  expect(await link.evaluate((el) => getComputedStyle(el, "::after").transformOrigin)).toMatch(/^0px /);
  expect(await link.evaluate((el) => getComputedStyle(el, "::after").transitionDuration)).toBe("0.16s");
  await link.hover();
  await expect.poll(underline).toBe("1");
});

test("with motion on, the serpent draws itself as the hero loads: sun rises, necks stroke in, heads follow", async ({
  page,
}) => {
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.goto("/");
  const serpent = page.getByRole("img", { name: /eight-headed serpent/ });
  const sun = serpent.locator(".mk-serpent-sun");
  const necks = serpent.locator(".mk-serpent-neck");
  const heads = serpent.locator(".mk-serpent-head");
  await expect(sun).toHaveCSS("animation-name", "mk-sun-rise");
  await expect(sun).toHaveCSS("animation-duration", "0.6s");
  // Each neck is one dash of its own length, drawn 60ms after the one before; its head fades in as it arrives.
  await expect(necks.first()).toHaveAttribute("pathLength", "1");
  await expect(necks.first()).toHaveCSS("animation-name", "mk-neck-draw");
  await expect(necks.nth(1)).toHaveCSS("animation-delay", "0.36s");
  await expect(heads.nth(1)).toHaveCSS("animation-name", "mk-head-in");
  await expect(heads.nth(1)).toHaveCSS("animation-delay", "0.58s");
  // The heads' <g> carries the fade, so each ellipse keeps its own rotate().
  await expect(heads.first().locator("ellipse")).toHaveAttribute("transform", /^rotate\(-60 /);
  // It all lands: the sun in place, every neck drawn, every head showing.
  await expect.poll(() => sun.evaluate((el) => getComputedStyle(el).translate)).toMatch(/^(none|0px)$/);
  await expect.poll(() => necks.last().evaluate((el) => getComputedStyle(el).strokeDashoffset)).toBe("0px");
  await expect.poll(() => paintedOpacity(heads.last())).toBe(1);
});

test("with motion on, the spec reveals line by line as real text, and the caret blinks then goes", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.goto("/");
  await expect(page.locator("html")).toHaveAttribute("data-motion", "on");
  const figure = page.getByRole("figure", { name: "You write a spec like this one" });
  const spec = figure.locator("blockquote");
  const lines = spec.locator(".mk-spec-line");
  const caret = spec.locator(".mk-spec-caret");
  // One span per sentence or rule, in order, none hidden from screen readers; together they are the spec.
  await expect(lines).toHaveCount(8);
  await expect(lines.first()).toHaveText("Build a code review app for our Python services.");
  await expect(lines.nth(2)).toHaveText("Rules: never log PII;");
  expect((await lines.allTextContents()).join("")).toMatch(EXAMPLE_SPEC_START);
  await expect(spec.locator("[aria-hidden]")).toHaveCount(1);
  await expect(caret).toHaveAttribute("aria-hidden", "true");
  // Below the fold it waits, with the caret on its first frame.
  await expect.poll(() => paintedOpacity(lines.last())).toBe(0);

  await figure.scrollIntoViewIfNeeded();
  // Opacity only, 110ms apart from 300ms.
  await expect(lines.nth(1)).toHaveCSS("animation-name", "mk-fade");
  await expect(lines.nth(1)).toHaveCSS("animation-delay", "0.41s");
  await expect(caret).toHaveCSS("animation-timing-function", "steps(1)");
  await expect(caret).toHaveCSS("animation-iteration-count", "3");
  await expect(caret).toBeVisible();
  await expect.poll(() => paintedOpacity(lines.last())).toBe(1);
  // After its three blinks the caret is gone.
  await expect(caret).toBeHidden({ timeout: 5_000 });
  await expect(caret).toHaveCSS("visibility", "hidden");
});

test("with motion on, Proof's after numbers count up behind their unchanged labels", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.goto("/");
  await expect(page.locator("html")).toHaveAttribute("data-motion", "on");
  const stats = page.locator("#proof dl[data-reveal]");
  const after = stats.getByLabel("after 100%");
  const figure = after.locator("[data-count]");
  // Record every value the counting figure shows once the stats come into view.
  await figure.evaluate((el) => {
    const seen: string[] = [];
    (window as unknown as { seen: string[] }).seen = seen;
    new MutationObserver(() => seen.push(el.textContent ?? "")).observe(el, { childList: true, characterData: true, subtree: true });
  });
  await stats.scrollIntoViewIfNeeded();
  await expect(stats).toHaveAttribute("data-inview", "");
  await expect.poll(() => page.evaluate(() => (window as unknown as { seen: string[] }).seen.at(-1))).toBe("100%");
  const seen = await page.evaluate(() => (window as unknown as { seen: string[] }).seen);
  // It climbs from the before value, 88%, never past the real value.
  expect(seen[0]).toBe("88%");
  const numbers = seen.map((v) => Number.parseInt(v, 10));
  expect(numbers).toEqual([...numbers].sort((a, b) => a - b));
  expect(Math.max(...numbers)).toBe(100);
  // Numbers that didn't change never count: the false alarms stay at 0%.
  await expect(stats.getByLabel("after 0%").locator("[data-count]")).toHaveText("0%");
  // The figure sits over an invisible copy of the final value, so its width never changes while it counts.
  await expect(after.locator("[aria-hidden] > .invisible")).toHaveText("100%");
  // The label and the screen-reader copy carry the real value the whole time.
  await expect(after).toHaveAttribute("aria-label", "after 100%");
  await expect(after.locator(".sr-only")).toHaveText("100%");
  await expect(stats.getByLabel(/^after /)).toHaveCount(3);
});

/** The element's own rotation in whole degrees, read from its computed transform (the inline rotate()). */
function transformDegrees(locator: Locator): Promise<number> {
  return locator.evaluate((el) => {
    const { a, b } = new DOMMatrix(getComputedStyle(el).transform);
    return Math.round((Math.atan2(b, a) * 180) / Math.PI);
  });
}

test("with motion on, the hero lands in the spec's order within 1.1s and keeps every stamp's own tilt", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.goto("/");
  const hero = page.locator('section[aria-labelledby="hero-heading"]');
  // Start and end of each load animation, in ms, from its computed delay and duration. [docs/motion-spec.md §2 Hero]
  const timing = (selector: string) =>
    hero.locator(selector).evaluateAll((els) =>
      els.map((el) => {
        const style = getComputedStyle(el);
        const start = Number.parseFloat(style.animationDelay) * 1000;
        return [Math.round(start), Math.round(start + Number.parseFloat(style.animationDuration) * 1000)];
      }),
    );
  const [kanji] = await timing(".mk-hero-brush");
  const [h1, lead, ctas] = await timing(".mk-hero-rise");
  const [stamp] = await timing(".mk-hero-stamp");
  const [badge] = await timing(".mk-hero-badge");
  const [postmark] = await timing(".mk-hero-postmark");
  const [shake] = await timing(".mk-hero-shake");
  expect([kanji, h1, lead, ctas, stamp, badge, postmark, shake].map(([start]) => start)).toEqual([
    0, 80, 180, 260, 240, 640, 760, 760,
  ]);
  // The postmark is the last to land, at 760 + 360ms: "about 1.1s" in the spec's own table.
  const ends = [kanji, h1, lead, ctas, stamp, badge, postmark, shake].map(([, end]) => end);
  expect(Math.max(...ends)).toBe(postmark[1]);
  expect(postmark[1]).toBeLessThanOrEqual(1120);

  // Once everything has played, each stamp sits on its own inline tilt: the press and slam never overwrite it.
  await hero.evaluate((el) => Promise.all(el.getAnimations({ subtree: true }).map((a) => a.finished)));
  expect(await transformDegrees(hero.locator(".mk-hero-stamp"))).toBe(-2);
  const mark = hero.locator(".mk-hero-postmark");
  expect(await transformDegrees(mark)).toBe(-12);
  expect(await paintedOpacity(mark)).toBeCloseTo(0.8, 5);
  await expect(hero.locator(".mk-hero-badge")).toHaveCSS("scale", /^(none|1)$/);
});

test("with motion on, the VERIFIED seal slams, rings and shakes once per page load", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.goto("/");
  await expect(page.locator("html")).toHaveAttribute("data-motion", "on");
  // Count every animation that starts on the seal (and its ::after ring) and on the intro it shakes.
  await page.evaluate(() => {
    const starts: string[] = [];
    (window as unknown as { starts: string[] }).starts = starts;
    for (const el of document.querySelectorAll("#proof .mk-proof-seal, #proof .mk-proof-shake")) {
      el.addEventListener("animationstart", (e) => {
        const event = e as AnimationEvent;
        if (event.target === el) starts.push(event.animationName + event.pseudoElement);
      });
    }
  });
  const stats = page.locator("#proof dl[data-reveal]");
  const seal = page.locator("#proof .mk-proof-seal");
  await stats.scrollIntoViewIfNeeded();
  await expect.poll(() => afterOpacity(seal)).toBe(0);
  await expect.poll(() => paintedOpacity(seal)).toBe(1);
  await seal.evaluate((el) => Promise.all(el.getAnimations({ subtree: true }).map((a) => a.finished)));
  // Scroll away and back twice: the reveal stays revealed and nothing plays again.
  for (let pass = 0; pass < 2; pass++) {
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.locator("footer").scrollIntoViewIfNeeded();
    await stats.scrollIntoViewIfNeeded();
  }
  await expect(stats).toHaveAttribute("data-inview", "");
  const starts = await page.evaluate(() => (window as unknown as { starts: string[] }).starts);
  expect(starts.sort()).toEqual(["mk-bleed::after", "mk-shake", "mk-slam"]);
  expect(await transformDegrees(seal)).toBe(10);
});

test("with motion on, buttons lift on hover, sink on press, and keep their focus ring", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.goto("/");
  const hero = page.locator('section[aria-labelledby="hero-heading"]');
  const primary = hero.getByRole("link", { name: "Start building" });
  const secondary = hero.getByRole("link", { name: "Watch the demo" });
  await hero.evaluate((el) => Promise.all(el.getAnimations({ subtree: true }).map((a) => a.finished)));

  // Keyboard focus: the existing 2px vermilion outline, plus the hover lift.
  await page.keyboard.press("Tab");
  await primary.focus();
  await expect(primary).toHaveCSS("outline-style", "solid");
  await expect(primary).toHaveCSS("outline-width", "2px");
  await expect(primary).toHaveCSS("outline-offset", "2px");
  await expect(primary).toHaveCSS("translate", "-2px -2px");
  await primary.blur();

  // Hover lifts onto a hard 4px offset shadow, never a blur (the primary turns ink on hover, so its shadow is vermilion).
  await secondary.hover();
  await expect(secondary).toHaveCSS("translate", "-2px -2px");
  await expect(secondary).toHaveCSS("box-shadow", /4px 4px 0px 0px$/);
  await primary.hover();
  await expect(primary).toHaveCSS("translate", "-2px -2px");
  await expect(primary).toHaveCSS("box-shadow", /4px 4px 0px 0px$/);
  // Press pushes it into the paper and drops the shadow; letting go off the button doesn't navigate.
  await page.mouse.down();
  await expect(primary).toHaveCSS("translate", "2px 2px");
  await expect(primary).toHaveCSS("box-shadow", /0px 0px 0px 0px$/);
  await page.mouse.move(0, 0);
  await page.mouse.up();
});

test("with motion on, a stamp card's offset shadow sits on its wrapper and follows the perforated edge", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.goto("/");
  const card = page.locator("#proof dl[data-reveal] > div").first();
  await card.scrollIntoViewIfNeeded();
  await expect.poll(() => paintedOpacity(card)).toBe(1);
  // The .perf mask is on the Stamp inside, never on the wrapper, so the wrapper's shadow is not masked away.
  expect(await card.evaluate((el) => getComputedStyle(el).maskImage)).toBe("none");
  expect(await card.locator("> .perf").evaluate((el) => getComputedStyle(el).maskImage)).not.toBe("none");
  // A drop-shadow (not a box-shadow) traces the perforations, and it grows to 6px on hover.
  await card.hover();
  await expect.poll(() => card.evaluate((el) => getComputedStyle(el).filter)).toMatch(/^drop-shadow\(.* 6px 6px 0px\)$/);
  await expect(card).toHaveCSS("box-shadow", "none");
});

test("at 375px with motion on, nothing scrolls sideways while the sections slide in", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.goto("/");
  await expect(page.locator("html")).toHaveAttribute("data-motion", "on");
  // .mk keeps its overflow-x clip, which is what holds back the slide-ins. [docs/motion-spec.md §7]
  await expect(page.locator(".mk").first()).toHaveCSS("overflow-x", "clip");
  const noSideScroll = () =>
    page.evaluate(() => {
      const root = document.documentElement;
      return root.scrollWidth <= root.clientWidth;
    });
  // Step down the page a screen at a time, so every reveal fires, and check mid-animation and once landed.
  const height = await page.evaluate(() => document.documentElement.scrollHeight);
  for (let y = 0; y <= height; y += 600) {
    await page.evaluate((top) => window.scrollTo(0, top), y);
    expect(await noSideScroll(), `no horizontal scroll at y=${y}`).toBe(true);
  }
  await page.waitForTimeout(1_200);
  expect(await noSideScroll(), "no horizontal scroll once everything has landed").toBe(true);
});

test("with reduced motion, nothing animates anywhere and hover changes colour without moving", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/");
  // Not one animation on the page, from first paint to the footer. [docs/motion-spec.md §5]
  expect(await page.evaluate(() => document.getAnimations().length)).toBe(0);
  await page.locator("footer").scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.getAnimations().length)).toBe(0);
  const primary = page.locator('section[aria-labelledby="hero-heading"]').getByRole("link", { name: "Start building" });
  await primary.hover();
  await expect(primary).toHaveCSS("translate", "none");
  await expect(primary).toHaveCSS("background-color", "rgb(27, 26, 23)");
  const card = page.locator("#proof dl[data-reveal] > div").first();
  await card.hover();
  await expect(card.locator("> .perf")).toHaveCSS("translate", "none");
});
