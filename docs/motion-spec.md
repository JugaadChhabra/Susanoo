# Motion spec: marketing landing page

Scope: `apps/web/app/(marketing)/page.tsx` and its sections in `apps/web/components/marketing/` and `apps/web/components/stamp/`. This is a spec only; no code has changed.
Problem: the page looks like a sheet of stamps but nothing on it moves like one. Motion should make every stamp, seal and form feel pressed onto paper, and then stay out of the way.

## 1. Principles

| Metaphor | Meaning in motion | Used for |
|---|---|---|
| **Press** | Starts slightly large and lifted, comes down fast, overshoots under size by about 4%, then settles | `Stamp`, `SerpentStamp`, pricing cards |
| **Thud** | One hard landing with a 1 to 2px shake of the surface underneath. No bounce loops. | `HankoSeal` (VERIFIED, Trust), `Postmark` |
| **Ink bleed** | A ring of colour that spreads out and fades behind a seal after it lands | Seal slams only |
| **Paper settle** | Slides up about 16px and straightens from about 1deg of tilt | Copy, cards, the list items in UseCases |

Rules:
1. Everything plays **once**. Nothing loops except the existing HowItWorks sequence.
2. Things land within about 1.1s of load or of coming into view. The longest single animation is 700ms.
3. The page uses **hard offset shadows**, so hover moves the object and grows the offset. It never adds blur or glow.
4. Animate only `transform` and `opacity`. Use the **individual `translate` / `scale` / `rotate` properties**, not `transform`. `Stamp tilt`, `HankoSeal rotate` and `Postmark rotate` set an inline `transform: rotate()`, and the individual properties combine with it instead of overwriting it.
5. Prefer **from-only keyframes**. The end state is then whatever the element already has: Postmark's `opacity-80`, the inline rotation and so on.

### Tokens (add to the `.mk` block in `app/globals.css`)

```css
.mk {
  --ease-ink: cubic-bezier(0.22, 1, 0.36, 1);     /* reveals: fast out, long soft tail */
  --ease-drop: cubic-bezier(0.55, 0, 0.75, 0.1);  /* the stamp falling: accelerates into the paper */
  --ease-settle: cubic-bezier(0.34, 1.56, 0.64, 1); /* small overshoot after impact */
  --ease-snap: cubic-bezier(0.2, 0, 0, 1);        /* hover and press */
  --dur-tap: 80ms;  --dur-hover: 160ms;  --dur-reveal: 520ms;
  --dur-press: 460ms;  --dur-slam: 560ms;  --dur-bleed: 700ms;
  --stagger: 70ms;  /* multiplied by --i on each item */
}
```

### Keyframes (also in `globals.css`)

```css
@keyframes mk-settle { from { opacity: 0; translate: 0 16px; rotate: -1deg; } }
@keyframes mk-rise   { from { opacity: 0; translate: 0 0.35em; } }
@keyframes mk-press {
  0%   { opacity: 0; scale: 1.16; translate: 0 -10px; animation-timing-function: var(--ease-drop); }
  55%  { opacity: 1; scale: 0.96; translate: 0 0;     animation-timing-function: var(--ease-settle); }
  100% { scale: 1; }
}
@keyframes mk-slam {
  0%   { opacity: 0; scale: 2.2; rotate: -12deg; animation-timing-function: var(--ease-drop); }
  60%  { opacity: 1; scale: 0.9; rotate: 0deg;   animation-timing-function: var(--ease-settle); }
  100% { scale: 1; rotate: 0deg; }
}
@keyframes mk-bleed { 0% { opacity: 0.4; scale: 0.9; } 100% { opacity: 0; scale: 1.45; } }
@keyframes mk-shake { 25% { translate: 1px 2px; } 60% { translate: -1px -1px; } }
@keyframes mk-brush { from { clip-path: inset(0 0 100% 0); } } /* vertical kanji only; see §5 */
```

Always set `animation-fill-mode: both` so a delayed item stays hidden until its turn.

## 2. Per-section plan

`--i` is the item index, set inline as `style={{ "--i": i }}`. "In view" means the item has `data-reveal` and gets `data-inview` from the observer in §4.

### Hero: `Hero()` in `sections.tsx` and `serpent-stamp.tsx`. Trigger: page load, CSS only.
| t (ms) | Element | Animation |
|---|---|---|
| 0 | Vertical `八岐退治` block (`div.vert` with the vermilion border) | `mk-brush` 600ms `--ease-ink`, drawn top to bottom like one brush stroke |
| 80 | `h1#hero-heading` | `mk-rise` `--dur-reveal` `--ease-ink`. Animate the whole h1. Do not split the vermilion span, because `inline-block` would change how the line wraps. |
| 180 | Lead `p` | `mk-rise`, same timing |
| 260 | CTA row (`div.flex.flex-wrap.gap-3.5`) | `mk-rise` |
| 240 | `SerpentStamp` outer `Stamp` (keeps its `tilt={-2}`) | `mk-press` `--dur-press` |
| 640 | Its `八岐大蛇` vertical badge | `mk-slam` at 0.6× scale range (`scale: 1.6` start), 380ms |
| 760 | `Postmark` | `mk-press` 360ms, ends at its own `opacity-80` and `rotate(-12deg)`. `SerpentStamp`'s parent wrapper gets `mk-shake` 180ms at the same moment. |

Phase 2 adds an SVG draw inside `SerpentStamp`. The sun `<circle>` rises (`translate: 0 40px` to 0, 600ms). Each neck path in `NECKS` gets `pathLength="1"` and animates `stroke-dashoffset` from 1 to 0 at `--i`×60ms. Head ellipses and eyes fade in after their neck. Wrap each head in a `<g>` before scaling it, because the ellipses already carry a `transform` attribute.

### Telegram: `Telegram()` in `sections.tsx`. Trigger: in view.
- `figure`: `mk-settle` 520ms, sliding in from the right (`translate: 24px 0`).
- The 10px vermilion shadow grows from 0. Move it off `box-shadow` onto an `::after` (`inset-0`, `bg-vermilion`, `-z-10`, final `translate: 10px 10px`) and animate its `translate` from `0 0`, 400ms, 200ms delay.
- Header bar (`bg-vermilion`, "SPEC FORM 01"): `mk-rise` at 120ms.
- **Line reveal (phase 2):** split `EXAMPLE_SPEC` on sentence and rule boundaries (`. ` and `; `) into inline `<span>`s, which are real text, in order, with nothing `aria-hidden`. Each span gets `mk-rise` (opacity only, no translate on inline text) at 300ms + `--i`×110ms. A block caret `▌` (`aria-hidden`) blinks with `steps(1)` 3 times and is then removed. Do not use character-by-character typing: it is slow to read and noisy for screen readers.

### HowItWorks: `how-it-works.tsx` (already a client component with an IntersectionObserver).
- In view: each `li` in both `StampSheet`s does `mk-settle` with `--i` = `index` (0 to 7) × 60ms. The row legend squares (`size-[18px]`) do `mk-press` 300ms before their row starts.
- **Lit head thud (CSS only, phase 1):** `li[aria-current="step"] > .perf { animation: mk-press 320ms both; }`. The animation restarts each time `active` moves, because the selector starts matching a new element. The existing `transition-colors duration-500` on `StampFace` still does the mustard fill. Add `translate: 0 -4px` on the lit stamp (`transition: translate var(--dur-hover) var(--ease-snap)`).
- The progress dot (`span.size-2.5`): `scale` pops from 1.6 to 1, 200ms, when it turns `bg-ink`.
- Reduced motion already disables lighting (`useReducedMotion`), so none of this fires. Keep that hook as the single source and move it to `lib/` if §4 needs it too.

### Proof: `Proof()` in `sections.tsx`. Trigger: in view on the `dl`.
1. Each stat `Stamp`: `mk-press` at `--i`×90ms.
2. Inside each stat, the `before` value (the `line-through` span) fades to its final state in 200ms. The `→` slides with `translate: -8px 0`. The `after` value (`.mk-display.text-5xl.text-vermilion`) does `mk-slam` with `scale` starting at 1.5, at 250ms + `--i`×90ms.
3. **VERIFIED seal** (`HankoSeal char="検証済" label="VERIFIED" rotate={10}`) does `mk-slam` `--dur-slam` at about 650ms, after the last stat lands. Its `rotate` keyframe starts at -12deg and combines with the inline `rotate(10deg)`. On impact:
   - an `::after` ring (`border-4 border-vermilion`, `inset-[-6px]`) plays `mk-bleed` `--dur-bleed`;
   - `.relative` (the intro wrapper) plays `mk-shake` 180ms.
4. Phase 2, optional: count up the `after` numbers. Write to a separate `aria-hidden` span so the existing `aria-label`s stay correct.

### UseCases: `UseCases()`. Trigger: in view.
- Live card (`Stamp` with `StampFace tone="vermilion"`): `mk-press` 460ms. The `査読` kanji does `mk-slam` (start `scale: 1.6`) at 300ms.
- Coming-soon `li`s (dashed border): `mk-settle` at 200ms + `--i`×80ms. They end at their own `opacity-80` (from-only keyframe).
- Hover: see §3, stamp cards (live card only).

### Trust: `Trust()` with `SEALS`. Trigger: in view on the `dl`.
- Each `HankoSeal` does `mk-slam` at `--i`×140ms and keeps its own `rotate` (−4, 3, −2, 5). Its `dt` and `dd` do `mk-rise` 120ms after their seal.
- One `mk-bleed` ring per seal. On the `round` shape the ring is `rounded-full`, on the others `rounded-md`. Copy the shape class onto the pseudo-element.
- Hover on the column (`div.flex.flex-col`): the seal's `rotate` animates to 0 and `scale` to 1.06, 160ms `--ease-snap`, as if picked up and straightened.

### Connectors: `Connectors()`. Trigger: in view on the `ul`.
- Each `li`: `mk-settle` at `min(--i, 7)`×50ms, so the stagger stops at 350ms however many connectors there are.
- `Available` tags do `mk-press` 260ms after their card. Dimmed cards (`tone="dim"`, `opacity-70`) only fade, with no press, and have no hover lift.

### Pricing: the new inline cards replacing `Plans()` (tiers come from `PLANS` in `lib/marketing`). Trigger: in view.
- Cards are `mk-press` at `--i`×110ms. The **featured** card goes last, with `scale` starting at 1.22, and its grid gets `mk-shake` on impact.
- If the featured card carries a seal or badge, it slams 200ms after the card lands.
- Feature `li`s with the `Check` icon: opacity only, at `--i`×40ms, capped at 8.
- Never animate the price text. Prices are blank until cfo.ai sets them, and an animated empty string looks broken.
- Hover: the stamp-card rule in §3. On the featured card the hover offset shadow is in mustard.

### DemoBand: `DemoBand()`. Trigger: in view.
- The vermilion block (`border-[3px] ... shadow-[12px_12px_0_var(--ink)]`) does `mk-settle` 520ms. Its ink shadow grows from 0 to 12px using the same `::after` technique as Telegram.
- "Open full screen" link: `mk-press` 300ms at 400ms, then the button hover and press in §3.
- Do **not** animate `DemoReplay`; it has its own timeline.

### Header: `site-chrome.tsx`, optional
- Nav links get an underline drawn with `scale: 0 1` to `1 1` from the left, 160ms. The "Start building" CTA uses the button rule in §3.

## 3. Hover and press micro-interactions

Apply to the `PRIMARY` and `SECONDARY` constants in `sections.tsx`, the header CTA, the UseCases and DemoBand paper buttons, and the HowItWorks pause button.

| State | Button | Stamp card (`Stamp` in Proof, UseCases live card, Connectors live, pricing) |
|---|---|---|
| Rest | `translate: 0 0`, offset shadow 0 | as it is now |
| Hover (`@media (hover: hover)`) | `translate: -2px -2px`, shadow `4px 4px 0 var(--ink)` (vermilion buttons) or `var(--vermilion)` (paper and outline buttons), `--dur-hover` `--ease-snap` | `translate: -3px -4px`, `rotate: -0.6deg`, the offset shadow behind it grows from 0 to 6px |
| Press (`:active`) | `translate: 2px 2px`, shadow 0, `--dur-tap`. The button is pushed into the paper. | `translate: 1px 1px`, shadow 0 |
| Focus-visible | The existing `FOCUS_RING` / `FOCUS_RING_DARK`, unchanged, plus the hover lift | same |

- **The `.perf` mask clips anything outside the `Stamp` box**, so a shadow on `Stamp` itself is invisible. Put the stamp-card shadow on a wrapper: the `li` or a new `div`, using `filter: drop-shadow(6px 6px 0 var(--ink))`, which follows the perforated edge. Alternatively, put it on `StampFace`, which has room inside the 14 to 18px perforated margin.
- Box-shadow transitions on small buttons are an accepted exception to "transform and opacity only": one element, under 200ms, triggered by hover. Large surfaces use the `::after` offset layer.
- Keep the existing colour swaps (`hover:bg-ink`, `hover:bg-mustard`) and run them on the same 160ms timing.

## 4. Implementation approach

**Recommendation: CSS keyframes and transitions, plus one tiny client observer. Add no Motion and no GSAP.**

- Everything above is a one-shot entrance, a stagger, a keyframe sequence or a hover state. CSS does all of these natively, and `tw-animate-css` is already imported.
- Nothing needs what the libraries add: springs driven by gestures, layout animation (FLIP), scrubbed timelines or SVG morphing.
- Motion would turn the server sections in `sections.tsx` into client components and add about 30 to 40 kB of client JS. GSAP plus ScrollTrigger adds about 35 kB and an imperative layer to maintain. Neither removes real work here, so under the project's dependency rule both are a no.
- Revisit only if a later feature needs gesture physics or layout animation, such as a monthly/annual toggle on pricing that reflows the cards.

**Observer**: a new `components/marketing/motion-root.tsx` (`"use client"`, about 25 lines), rendered once inside `MarketingShell`:
1. On mount, if `(prefers-reduced-motion: no-preference)` matches, it sets `document.documentElement.dataset.motion = "on"`.
2. One shared `IntersectionObserver` (`rootMargin: "0px 0px -12% 0px"`, `threshold: 0.15`) watches every `[data-reveal]`. On entry it sets `data-inview` and calls `unobserve`, so each reveal plays once.
3. Sections stay server components. They only gain `data-reveal` attributes and `--i` inline styles.

**Gating CSS**: hidden states exist only when JS has run and motion is allowed.
```css
@media (prefers-reduced-motion: no-preference) {
  [data-motion="on"] [data-reveal]:not([data-inview]) .mk-anim { animation-play-state: paused; }
  [data-motion="on"] [data-reveal][data-inview] .mk-anim { animation-play-state: running; }
}
```
In-view items use paused animations with `fill-mode: both`. With JS off there is no `data-motion`, so nothing is paused and every animation simply plays on load. With reduced motion, everything is visible immediately. Never use `opacity-0` as a static class.

`tw-animate-css` (`animate-in fade-in slide-in-from-bottom-*`) is fine for plain text blocks. Do **not** use it on `Stamp`, `HankoSeal` or `Postmark`: its keyframes set `transform` and wipe out their inline rotation. Use the `mk-*` keyframes on those.

## 5. Accessibility and performance

- **Reduced motion**: inside `.mk`, `@media (prefers-reduced-motion: reduce)` sets `animation: none` and `transition: none` on everything. From-only keyframes then mean every element sits in its final state. HowItWorks keeps its `useReducedMotion` behaviour, so there is no lighting and no pause button. Hover still changes colour but does not move.
- **No layout shift**: only `translate`, `scale`, `rotate` and `opacity` move, so none of them affect layout. No heights or margins are animated, and no element switches between `display: none` and visible. Target CLS 0.00.
- **Progressive enhancement**: content is in the server HTML and visible by default. Hiding depends on the observer having run (§4). The hero uses load-time CSS only, so it never waits for hydration.
- **Screen readers**: no text is replaced or typed in. The spec text stays one readable sequence. Decorative layers (rings, caret, `::after` shadows) are pseudo-elements or `aria-hidden`.
- **Exceptions to "transform and opacity only"**, all one-shot and on small elements: `clip-path` on the hero kanji block, `stroke-dashoffset` on the serpent necks (phase 2), and box-shadow on buttons when hovered.
- **Performance**: no scroll listeners. Each element is observed once and unobserved after it reveals. Use `will-change` only through `.mk-anim` while an animation runs, never globally. At most about 8 elements animate at once. A mid-range Android device should hold 60fps.

## 6. Rollout

**Phase 1: about 15 minutes, CSS only, no new files.** Low risk, because nothing is hidden before JS runs.
1. Add the tokens, the `mk-*` keyframes and the reduced-motion block to `app/globals.css`.
2. Hero load sequence (§2 Hero table) via arbitrary classes such as `[animation:mk-rise_var(--dur-reveal)_var(--ease-ink)_80ms_both]` in `sections.tsx` and `serpent-stamp.tsx`.
3. Button hover and press on `PRIMARY` and `SECONDARY`, plus the header CTA.
4. The HowItWorks lit-head thud through the `aria-current` selector, plus `translate` on the lit stamp.
5. Stamp-card hover lift, without the shadow: `translate` and `rotate` only.

**Phase 2: about 1.5 to 2 hours.**
1. `motion-root.tsx` and the gating CSS; `data-reveal` and `--i` added in every section.
2. In-view choreography for Telegram, HowItWorks, Proof (VERIFIED slam, bleed, shake), UseCases, Trust, Connectors, Pricing and DemoBand.
3. The `::after` offset-shadow layers (Telegram, DemoBand, stamp cards via `drop-shadow`).
4. Serpent SVG draw, Telegram line reveal, the optional count-up in Proof.

Phase 2 should land before the feature freeze (Mon 12 Oct, 13:00 IST) or not at all. Phase 1 counts as polish, not a feature.

## 7. Acceptance checklist

- [ ] The hero is fully composed within 1.1s of load. Kanji, copy, CTAs, serpent stamp and postmark land in the order in §2.
- [ ] `SerpentStamp` still ends at `-2deg`, Postmark at `-12deg` and 80% opacity, and VERIFIED at `10deg`, because inline rotations are preserved.
- [ ] Each HowItWorks step thuds as it lights. Pausing or reduced motion stops it.
- [ ] The VERIFIED seal slams after the three stats, with one ink ring and one shake, once per page load.
- [ ] The four Trust seals land in order and each keeps its own tilt.
- [ ] Buttons lift on hover and sink on press. Focus rings are unchanged and visible.
- [ ] Stamp-card shadows follow the perforated edge and are not clipped by `.perf`.
- [ ] Reduced motion (OS setting or DevTools emulation): no movement anywhere, and all content visible on first paint.
- [ ] JS disabled: every section is visible, and load animations play or show the final state.
- [ ] Lighthouse on `/`: CLS is 0, and no new long tasks come from motion.
- [ ] The DevTools Performance panel shows no layout or paint during reveals, apart from the listed exceptions.
- [ ] At 375px width: no horizontal scroll caused by slide-ins (the `overflow-x-clip` on `.mk` stays), and staggers still read well in a single column.
- [ ] `package.json` has no new dependencies, and `make lint` passes.
