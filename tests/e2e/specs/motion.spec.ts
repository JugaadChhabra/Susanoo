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
      ];
      for (const target of targets) {
        await target.scrollIntoViewIfNeeded();
        await expect(target).toBeVisible();
        await expect.poll(() => paintedOpacity(target)).toBe(1);
      }
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
