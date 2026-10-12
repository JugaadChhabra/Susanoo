# Susanoo

> AI agents are a many-headed mess. Susanoo tames them.

Describe a product in plain English. Susanoo's eight agents, the heads of Orochi, build it as a running web app
with its own AI agent, attack it with realistic scenarios, diagnose why it fails, fix the right layer, and prove
the fix with traces. Built for neatHack 2026 by Muaaz Shaikh and Jugaad Chhabra.

- Product requirements: [`PRD.md`](PRD.md) / [`PRD.pdf`](PRD.pdf), the single source of truth
- Plans: [`IMPLEMENTATION_MUAAZ.md`](IMPLEMENTATION_MUAAZ.md), [`IMPLEMENTATION_JUGAAD.md`](IMPLEMENTATION_JUGAAD.md)
- Evidence for judges: [`EVIDENCE.md`](EVIDENCE.md)

## How we use Entire
- **Inside Susanoo:** every Fixer patch on a generated repo is a commit with an Entire checkpoint (the failure, the diagnosis, the rationale). Example: hero-7's kept fix `60edf30`, checkpoint `01M4RB1D8JPSNQYGPBW8V7DCMT`, in [susanoo-apps/python-code-review-hero7](https://github.com/susanoo-apps/python-code-review-hero7); Entire's own view of it is in [`docs/evidence/entire-hero7-checkpoints.txt`](docs/evidence/entire-hero7-checkpoints.txt).
- **On this repo:** Entire was switched on late, on Mon 12 Oct at 21:14 IST, not from the first commit, so live checkpoints exist only from then on. `--import-history` brought in the Claude Code history that was still on disk: 186 turns of the main build session (it starts Sun 11 Oct at 17:53 IST) and of today's session. Those are read-only imported checkpoints (`entire checkpoint list` shows them as `[imported]`). They stay local (`refs/entire/*`, session pushing off) because the transcripts contain personal contact details.
- **entire-graph of Susanoo:** [`docs/evidence/entire-graph-susanoo.txt`](docs/evidence/entire-graph-susanoo.txt) (graph size, plus `entire graph search` results for "where the Diagnostician picks a layer" and "how the held-out set is kept from the Fixer").
- `.githooks/*.pre-entire` are our identity and attribution guards; Entire's git hooks call them, so both run on every commit and push. The hooks do nothing if the `entire` binary isn't installed.

## Getting started (team)
```bash
git clone https://github.com/JugaadChhabra/Susanoo && cd Susanoo
git config user.name "<your GitHub username>"   # MuaazSM or JugaadChhabra
bash scripts/setup-dev.sh                        # hooks, identity check, .env
```
Then open Claude Code in the repo (it reads `CLAUDE.md`) and follow your `PROMPTBOOK_*.md` from Phase 0.

_Live link, architecture and demo video are added during the hackathon._
