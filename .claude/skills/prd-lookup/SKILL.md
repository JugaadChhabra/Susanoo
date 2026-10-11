---
name: prd-lookup
description: Find what the Susanoo PRD says before building, changing or deciding anything. Use at the start of every task, whenever a requirement is unclear, or when a prompt cites [PRD › Section].
---

# PRD lookup

`PRD.md` (repo root) is the single source of truth. `PRD.pdf` is the same content for people.

## Steps
1. Find the section: `grep -n '^## ' PRD.md` lists every heading. Citations look like **[PRD › Section name]**, and the text after `›` matches a `## ` heading exactly (sub-parts are bold labels inside that section).
2. Read the **whole** section, not just the matching line. Sections are short, and tables and diagrams carry the details.
3. Cross-check the owner's plan: `IMPLEMENTATION_MUAAZ.md` or `IMPLEMENTATION_JUGAAD.md`. Find the step that cites this section.
4. State back, in one or two lines, what the PRD requires and which IMPLEMENTATION step this is, before writing code.

## When the PRD is silent or ambiguous
- Don't invent a requirement. Pick the smallest choice that satisfies the cited section, and **ask the human** if the choice would be visible to users or judges.
- Never edit `PRD.md` yourself to "fix" scope. Scope changes go into the live PRD doc first; then the human re-exports `PRD.md` and `PRD.pdf`.

## Sections you'll use most
- Contracts (locked): schemas, event shape, head interface, scoring rule
- The eight heads of Orochi; LangGraph flow and shared state; Improvement loop and guardrails
- Intake: questions and approved scenarios; Testing environments; Connectors and API budget
- Data model; Deployment; Tech stack; Repository structure; Team and ownership
- Frontend (marketing site + app) and API; Non-technical users
- Before development: first-hour checklist; Timeline, owners, risks
