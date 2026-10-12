# PROMPTBOOK — Landing page motion (temporary)

One prompt per ticket for the landing page motion work in `docs/motion-spec.md`. The tickets are GitHub issues #35–#43 on `JugaadChhabra/Susanoo`, labelled `ready-for-agent` and `motion`. **This file is temporary: ticket #42 deletes it in its final commit.** The spec stays.

**How to use**
- **Automatically:** `PROMPTBOOK=PROMPTBOOK_MOTION.md LABEL=motion scripts/run-tickets.sh`. Same flow as every ticket before: one fresh headless session per unblocked issue, `/mattpocock-skills:implement` with its prompt from this file, then verify, rebase, fast-forward `main`, push and close. `DRY_RUN=1` prints the frontier and prompts first.
- **By hand:** new session, then `/mattpocock-skills:implement` followed by the ticket's prompt.
- Run the motion tickets **with** `LABEL=motion`. The default runner reads `PROMPTBOOK_JUGAAD.md`, finds no prompt for #35–#43 and stops.
- Order (dependencies decide, lowest number first): #43 → #35 → #36 → #37 → #38 → #39 → #40 → #41 → #42.
- Your git identity must be `JugaadChhabra` (`make check-identity`).

---

## Rules for every motion ticket

These apply to every prompt below; each prompt repeats the short form.

- **Keep the session lean.** Read only the spec sections the ticket names (e.g. `sed -n '/^### Proof/,/^### UseCases/p' docs/motion-spec.md`) plus §1, §4 and §5; don't read the whole spec or the whole repo. Read `apps/web/components/marketing/sections.tsx` by function, not end to end.
- **No new dependencies.** CSS keyframes and transitions only, plus the one observer from #36. No Motion, no GSAP (spec §4).
- **Animate only `translate` / `scale` / `rotate` / `opacity`**, as individual properties, with from-only keyframes so inline rotations survive. Listed exceptions only (spec §5).
- **Never hide content statically.** Reduced motion and JS off must show every final state on first paint.
- Sections stay server components; only `motion-root.tsx` (from #36) is a client component. `how-it-works.tsx` is already a client component.
- Verify with `make lint`, `make test` and `cd apps/web && npx next build`. From #36 on, also `make e2e` (Playwright: `reducedMotion: 'reduce'` and `javaScriptEnabled: false` on `/`).
- Commit area is `web`, e.g. `feat(web): hero presses in on load`. Body cites `docs/motion-spec.md §<n>` and [PRD › Marketing site].

---

## Phase 1

### #43 — Motion foundation and hero entrance
```
Use the frontend-screen skill. Read docs/motion-spec.md §1, §2 "Hero" (the table only; the phase 2 SVG paragraph is #41), §5 and §6 Phase 1.
1. Add the motion tokens, every mk-* keyframe from §1 and the reduced-motion block (§5) to the `.mk` block in apps/web/app/globals.css.
2. Build the hero load sequence in Hero() (apps/web/components/marketing/sections.tsx) and apps/web/components/marketing/serpent-stamp.tsx, using arbitrary animation classes or small named classes in globals.css. CSS only: it must not wait for hydration. Use `animation-fill-mode: both` throughout.
3. Keep inline rotations: Stamp tilt -2deg, Postmark -12deg and its opacity-80. Don't animate the vermilion span inside the h1 separately.
Check it: next build passes. In the final report, describe the order you'd see on load and confirm reduced motion shows the hero immediately.
Rules: only the spec sections named, no new deps, individual transform properties, nothing statically hidden.
```

### #35 — Hover and press for buttons and stamp cards
```
Use the frontend-screen skill. Read docs/motion-spec.md §1 (tokens, rules) and §3.
1. PRIMARY and SECONDARY in apps/web/components/marketing/sections.tsx, the header CTA in site-chrome.tsx, the UseCases and DemoBand paper buttons and the HowItWorks pause button: on hover (inside @media (hover: hover)) lift -2px -2px with a hard 4px offset shadow (ink on vermilion buttons, vermilion on paper and outline buttons); on :active sink 2px 2px with no shadow. Existing colour swaps use the same 160ms timing. FOCUS_RING / FOCUS_RING_DARK unchanged.
2. Stamp cards (Proof stats, the live UseCases card, live Connectors, pricing cards): on hover translate -3px -4px and rotate -0.6deg; the 6px offset shadow sits on a wrapper using filter: drop-shadow so the .perf mask doesn't clip it (§3 has the alternatives). Dimmed connector cards get no lift.
3. Reduced motion: colour still changes, nothing moves.
Rules: only the spec sections named, no new deps, no blur or glow, individual transform properties.
```

### #36 — Scroll reveal, proven on the spec form
```
Use the frontend-screen skill. Read docs/motion-spec.md §2 "Telegram" (not the line-reveal paragraph; that's #41), §4 and §5.
1. Create apps/web/components/marketing/motion-root.tsx ("use client", about 25 lines) exactly as §4 describes: set data-motion="on" on <html> only if (prefers-reduced-motion: no-preference) matches; one shared IntersectionObserver (rootMargin "0px 0px -12% 0px", threshold 0.15) sets data-inview on each [data-reveal] and unobserves it. Render it once inside MarketingShell.
2. Add the §4 gating CSS to globals.css. Reveal animations use a .mk-anim class with fill-mode both. Never use a static opacity-0.
3. Telegram(): the figure settles in from the right; its 10px vermilion shadow moves off box-shadow onto an ::after layer whose translate grows from 0 0 to 10px 10px (400ms, 200ms delay); the header bar rises at 120ms.
4. Add a Playwright spec in tests/e2e/specs (follow the existing specs and README) that loads / with reducedMotion 'reduce' and with javaScriptEnabled false and asserts that the hero heading, the spec form and the Trust section are visible with opacity 1.
Rules: only the spec sections named, no new deps, sections stay server components (they only gain data-reveal and --i), nothing statically hidden. make e2e must pass.
```

## Phase 2: in-view choreography

### #37 — How it works: heads thud as they light
```
Use the frontend-screen skill. Read docs/motion-spec.md §2 "HowItWorks" and §6 Phase 1 step 4. Code: apps/web/components/marketing/how-it-works.tsx and apps/web/components/stamp/stamp-sheet.tsx.
1. In view (data-reveal from #36): each li in both StampSheets settles in with --i = index (0–7) × 60ms; row legend squares press in 300ms before their row.
2. Lit head, CSS only: li[aria-current="step"] > .perf plays mk-press 320ms, so it replays whenever the active step moves; the lit stamp lifts translate 0 -4px on the hover timing. Keep the existing transition-colors fill.
3. The progress dot pops from scale 1.6 to 1 (200ms) when it turns bg-ink.
4. useReducedMotion stays the single source; if #36 needed it elsewhere, move it to apps/web/lib/. With reduced motion or paused, nothing thuds or lifts.
Rules: only the spec sections named, no new deps, individual transform properties.
```

### #38 — Proof: stats press and VERIFIED slams
```
Use the frontend-screen skill. Read docs/motion-spec.md §2 "Proof" steps 1–3 (the count-up, step 4, is #41). Code: Proof() in apps/web/components/marketing/sections.tsx, apps/web/components/stamp/hanko-seal.tsx.
1. data-reveal on the dl. Each stat Stamp plays mk-press at --i × 90ms; inside, the line-through "before" value fades (200ms), the arrow slides from -8px, and the "after" value plays mk-slam starting at scale 1.5, at 250ms + --i × 90ms.
2. The VERIFIED HankoSeal plays mk-slam (--dur-slam) at about 650ms. Its rotate keyframe starts at -12deg and ends at the seal's own inline 10deg. On impact: an ::after ring (border-4 border-vermilion, inset -6px) plays mk-bleed, and the intro wrapper plays mk-shake 180ms. Once per page load.
3. Leave the existing aria-labels alone; decorative layers are pseudo-elements or aria-hidden.
Rules: only the spec sections named, no new deps, from-only keyframes, nothing statically hidden.
```

### #39 — Use cases and Trust seals land
```
Use the frontend-screen skill. Read docs/motion-spec.md §2 "UseCases" and "Trust". Code: UseCases() and Trust() (with SEALS) in apps/web/components/marketing/sections.tsx.
1. UseCases, in view: the live card plays mk-press 460ms and its 査読 kanji plays mk-slam (start scale 1.6) at 300ms; the coming-soon li's settle at 200ms + --i × 80ms and end at their own opacity-80.
2. Trust, data-reveal on the dl: each HankoSeal plays mk-slam at --i × 140ms and keeps its own rotate (−4, 3, −2, 5); its dt and dd rise 120ms after it. One mk-bleed ring per seal, rounded-full on the round shape and rounded-md on the others (copy the shape class onto the pseudo-element).
3. Hovering a Trust column (hover-capable devices only) animates the seal's rotate to 0 and scale to 1.06, 160ms --ease-snap.
Rules: only the spec sections named, no new deps, individual transform properties, nothing statically hidden.
```

### #40 — Connectors, pricing, demo band and nav underline
```
Use the frontend-screen skill. Read docs/motion-spec.md §2 "Connectors", "Pricing", "DemoBand" and "Header". Code: Connectors(), Plans() (or the pricing cards that replaced it; tiers from PLANS in apps/web/lib/marketing.ts) and DemoBand() in sections.tsx, plus site-chrome.tsx.
1. Connectors, data-reveal on the ul: each li settles at min(--i, 7) × 50ms; Available tags press 260ms after their card; tone="dim" cards only fade, with no press and no hover lift.
2. Pricing: cards press at --i × 110ms; the featured card lands last, starting at scale 1.22, and its grid plays mk-shake on impact; a seal or badge on it slams 200ms later; feature li's with the Check icon fade only at --i × 40ms, capped at 8. Never animate the price text. The featured card's hover offset shadow is mustard (builds on #35).
3. DemoBand: the vermilion block settles 520ms and its 12px ink shadow grows via an ::after layer (same technique as Telegram in #36); "Open full screen" presses at 400ms. Do not animate DemoReplay.
4. Header nav links draw an underline from the left (scale 0 1 to 1 1, 160ms) on hover.
Rules: only the spec sections named, no new deps, individual transform properties, nothing statically hidden.
```

### #41 — Motion flourishes: serpent draw, spec line reveal, count-up
```
Use the frontend-screen skill. Read docs/motion-spec.md §2 "Hero" (the phase 2 paragraph), "Telegram" (Line reveal) and "Proof" step 4.
1. Serpent (apps/web/components/marketing/serpent-stamp.tsx): the sun <circle> rises translate 0 40px → 0 over 600ms; each path in NECKS gets pathLength="1" and animates stroke-dashoffset 1 → 0 at --i × 60ms; head ellipses and eyes fade in after their neck. Wrap each head in a <g> before scaling, because the ellipses already carry a transform attribute. Part of the hero load sequence (CSS only).
2. Telegram line reveal: split EXAMPLE_SPEC on ". " and "; " into inline <span>s of real text, in order, none aria-hidden; each fades in (opacity only) at 300ms + --i × 110ms. A block caret ▌ (aria-hidden) blinks with steps(1) 3 times and is then removed. No character-by-character typing.
3. Proof count-up: count the "after" numbers up in a separate aria-hidden span; the existing aria-labels stay correct. If this needs a client component, keep it as small as the one in #36 and render it only inside the stat.
4. Reduced motion and JS off: the full serpent, the full text and the final numbers show immediately. Extend the #36 Playwright spec to cover them.
Rules: only the spec sections named, no new deps, nothing statically hidden. make e2e must pass.
```

## Wrap-up

### #42 — Motion acceptance pass and promptbook cleanup
```
Read docs/motion-spec.md §7 (and only the sections an item points to). Go through every checklist item on a production build of apps/web:
- Hero order and timing, preserved rotations (SerpentStamp -2deg, Postmark -12deg at 80%, VERIFIED 10deg), HowItWorks thud and pause, the VERIFIED slam happening once, the Trust seal order, button and card hover and press, stamp-card shadows not clipped.
- Reduced motion and JS off: run make e2e (the specs from #36 and #41); add any missing assertions.
- Lighthouse on / (`npx lighthouse` against `next start`, headless Chrome, mobile and desktop): CLS 0 and no new long tasks from motion. Put the numbers in the final report.
- At 375px: no horizontal scroll (add a Playwright assertion: scrollWidth <= clientWidth).
- No new runtime dependency in apps/web/package.json; make lint passes.
Fix each failure in this ticket with the smallest change. List anything you can't fix in the final report rather than faking it.
Last commit: delete PROMPTBOOK_MOTION.md (`chore(docs): drop the motion promptbook, all motion tickets done`) and remove the PROMPTBOOK_MOTION example line from the usage comment in scripts/run-tickets.sh. Keep the PROMPTBOOK/LABEL options themselves. Keep docs/motion-spec.md.
```
