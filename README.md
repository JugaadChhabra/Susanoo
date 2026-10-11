# Susanoo

> AI agents are a many-headed mess. Susanoo tames them.

Describe a product in plain English. Susanoo's eight agents, the heads of Orochi, build it as a running web app
with its own AI agent, attack it with realistic scenarios, diagnose why it fails, fix the right layer, and prove
the fix with traces. Built for neatHack 2026 by Muaaz Shaikh and Jugaad Chhabra.

- Product requirements: [`PRD.md`](PRD.md) / [`PRD.pdf`](PRD.pdf), the single source of truth
- Plans: [`IMPLEMENTATION_MUAAZ.md`](IMPLEMENTATION_MUAAZ.md), [`IMPLEMENTATION_JUGAAD.md`](IMPLEMENTATION_JUGAAD.md)
- Evidence for judges: [`EVIDENCE.md`](EVIDENCE.md)

## Getting started (team)
```bash
git clone https://github.com/JugaadChhabra/Susanoo && cd Susanoo
git config user.name "<your GitHub username>"   # MuaazSM or JugaadChhabra
bash scripts/setup-dev.sh                        # hooks, identity check, .env
```
Then open Claude Code in the repo (it reads `CLAUDE.md`) and follow your `PROMPTBOOK_*.md` from Phase 0.

_Live link, architecture and demo video are added during the hackathon._
