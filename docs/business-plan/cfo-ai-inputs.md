# What to enter in cfo.ai

[PRD › Business model (inputs for cfo.ai)] and [PRD › Sponsor integrations and evidence plan]: the judged artefact is an **exported business plan (pricing, costs, revenue, runway)** in `docs/business-plan/`. cfo.ai needs a person's account, so this sheet is everything to type in; detailed arithmetic is in `cost-inputs.md`.

## 1. Measured unit cost (real runs, Mon 12 Oct)

| Run | Spec | Scenarios scored | Loop rounds | Model cost |
|---|---|---|---|---|
| hero-2 | code review | 28 | 2 (1 fix kept) | $1.75 |
| hero-3 | code review | 34 | 1 | $1.43 |
| hero-7 | code review | 39 | 1 (1 fix kept) | $1.75 |
| backup-2 | test review | 32 | 0 (all passed) | $0.89 |

- **Model cost per hardened agent (one full run): about $1.50 on average (range $0.89–$1.75)**, under the $2 per-run cap.
- **Model cost per extra improvement round:** about $0.35.
- **Sandbox compute:** measured locally only (a few container-minutes per Level 2 check; the hero runs above used Level 1). Enter **$0.05 per run** as a placeholder until Jugaad reads EC2 usage.
- **Haiku price** in `llm.py` is a placeholder; it only affects scenario drafting (a small share).

## 2. Fixed monthly costs (Jugaad to confirm from the bills)

| Item | Enter | Source |
|---|---|---|
| EC2 (api, worker, Caddy, generated apps) | TODO(Jugaad): instance price | AWS bill |
| Supabase | $0 (free) or $25 (Pro) | Supabase plan |
| Vercel | $0 (Hobby) or $20 (Pro) | Vercel plan |
| Domain + Cloudflare | ~$1/month | registrar |
| neatlogs, Entire | $0 during the hackathon | plans |

## 3. Pricing (proposal: Muaaz and Jugaad decide; the PRD leaves prices blank until cost per run is measured)

Targets ~85% gross margin on runs (cost ≈ $1.55 per run incl. sandbox).

| Plan | Price | Includes | Overage |
|---|---|---|---|
| Free | $0 | 3 runs per month, demo templates | none |
| Team | $49 / month | 30 runs per month, private repos, custom policies | $3 per extra run |
| Enterprise | from $1,500 / month | SSO, self-hosted runners, custom domains, support, volume runs | contract |

Unit economics at these prices: a Team customer using all 30 runs costs ~$47 in model + sandbox spend, so heavy users sit near break-even and average users (≈12 runs) give ~62% margin; raise the Team price or lower included runs if cfo.ai shows thin margin.

## 4. Scenarios to build in cfo.ai (base and growth)

| | Base | Growth |
|---|---|---|
| Paying Team customers, month 12 | 40 | 150 |
| Enterprise customers, month 12 | 1 | 4 |
| Average runs per Team customer / month | 12 | 15 |
| Free users (cost only) | 300 | 1,500, each using ~2 runs |
| Team (hackathon-to-company) | 2 founders, no salary for 6 months | 2 founders + 1 engineer from month 6 |
| Starting cash | TODO (e.g. $0 or a small grant) | same |

Free-tier runs are pure cost (~$3 per active free user per month): model it explicitly, it drives burn.

## 5. Steps in cfo.ai (Muaaz)

1. Sign up at cfo.ai (PRD: cancel the subscription right after).
2. Create a company "Susanoo"; enter the pricing in section 3 as products.
3. Enter costs: per-run variable cost $1.55; fixed monthly costs from section 2.
4. Build the two scenarios in section 4 (customers, ARPU, gross margin, burn, runway).
5. Export the plan (PDF or sheet) into `docs/business-plan/` and link it in `EVIDENCE.md` §4.
