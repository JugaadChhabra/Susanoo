# cfo.ai cost inputs (measured)

Source: [PRD › Business model (inputs for cfo.ai) › Cost drivers]. Every number below is taken from, or computed from, **one run**: `hero-2` (spec `specs/code-review.md`, run locally with `scripts/run_local.py`, Mon 12 Oct 2026). Raw data: `docs/evidence/hero-2-run.json`. This is a sample of one, so treat the figures as a first estimate, not an average. Prices for the plans are deliberately blank in the PRD and are not set here.

## 1. What is measured and what is not

| Driver (PRD) | Status | Notes |
|---|---|---|
| Anthropic cost, whole run | **Measured** | `cost_usd` = **$1.750983** |
| Anthropic cost per iteration | **Measured** (cumulative checkpoints) | Three `metrics` events carry a running total; see section 3 |
| Anthropic cost per head | **Not measured directly** | The run stores one running total, not a per-head or per-call figure. Section 4 gives estimates and says how they were derived |
| Tokens per head | **Not in the saved run** | `hero-2-run.json` has no token counts. `llm.call` returns `input_tokens` / `output_tokens` per call, but they are only on the neatlogs spans. TODO(Muaaz): read them from the neatlogs dashboard / export for the hero-2 trace ids and add them here |
| Sandbox compute (EC2 minutes per container) | **Not measured** | hero-2 ran on a laptop with in-process mocks; no generated-app container was started. TODO(Muaaz/Jugaad): measure on the live EC2 run (container start to stop, from Docker or the worker logs) |
| Fixed costs (hosting, database, domains) | **Not measured** | TODO(Jugaad): enter from the Vercel, EC2, Supabase and Cloudflare bills |
| Iterations | Measured | 2 loop rounds (round 1: a test change rejected at the gate, no fix written; round 2: one patch kept), 3 scored passes (iterations 0, 1, 2), plus one final pass; stop reason `no_diagnosis` |

## 2. Model prices used

From `packages/core/susanoo/integrations/llm.py` (`PRICING_USD_PER_MTOK`, USD per 1M tokens). `cost_usd` in the run is computed from this table.

| Model | Used by | Input | Output | Confidence |
|---|---|---|---|---|
| `claude-opus-5-5` | Planner, Diagnostician | $4.00 | $20.00 | Published rate (per the code comment, 2026-09) |
| `claude-sonnet-5-5` | Builder, Agent Smith, Fixer, **and the generated agent itself** (`AGENT_MODEL = llm.SONNET`, so every Runner scenario call is Sonnet) | $2.00 | $10.00 | Published rate (per the code comment) |
| `claude-haiku-5-5` | Simulator, translate modules | $1.00 | $5.00 | **PLACEHOLDER.** The code carries the `claude-haiku-4-5` rate because no published Haiku 5.5 rate was found. Confirm before relying on any Simulator or translate cost |

If the Haiku rate changes, only the Simulator's share of the setup cost (section 4, row A) moves; the measured totals above were computed with the placeholder.

## 3. Cost per iteration (measured)

The Runner and Verifier `metrics` events carry the running total `cost_usd` at that moment (`events[].payload.cost_usd`, also `metrics[].cost_usd`). Differences between consecutive totals give the cost of each stretch.

| Stretch | Events (seq) | What ran | Running total | Cost of this stretch |
|---|---|---|---|---|
| Setup + baseline (iteration 0) | 1 to 22 | Planner, Builder, Agent Smith, Simulator, Runner pass over 32 scenarios | $0.821175 | $0.821175 |
| Round 1 (iteration 1) | 23 to 30 | Diagnostician (test cause), human gate (rejected, no fix written), Runner pass over 28 scenarios | $1.163223 | 1.163223 - 0.821175 = **$0.342048** |
| Round 2 (iteration 2) | 31 to 37 | Diagnostician (prompt/policy cause), Fixer (patch-1, 3 lines), Verifier re-run | $1.517075 | 1.517075 - 1.163223 = **$0.353852** |
| Final pass | 38 to 44 | Runner pass over 28 scenarios; Diagnostician had no failures to explain | $1.750983 | 1.750983 - 1.517075 = **$0.233908** |
| **Total** | | | **$1.750983** | 0.821175 + 0.342048 + 0.353852 + 0.233908 = **1.750983** (checks) |

Other derived figures:
- Share of the per-run cap: the default cap is `$2.00` (`guards/budget.py`, `DEFAULT_RUN_COST_CAP_USD`). 1.750983 / 2.00 = **87.5%** of the cap.
- Cost of a loop round (diagnose + fix + verify): $0.342048 and $0.353852, about **$0.35 per round**.
- Cost of a baseline run excluding the loop: $0.821175 (iteration 0 total).

## 4. Cost per head (estimates, not measurements)

The final stretch is the one clean reading: it is a Runner pass and nothing else (event 42 says the Diagnostician had no failing scenarios to explain, so it should have made no model call; this is inferred from the message, not from a trace). So one Runner pass over 28 scenarios cost **$0.233908**, i.e. 0.233908 / 28 = **$0.00835 per scenario** (Sonnet, tool loop included). Using that as the price of a pass in the other stretches:

| Head | Model | Estimate | Arithmetic | Reliability |
|---|---|---|---|---|
| Runner (one pass, 28 scenarios) | Sonnet | $0.233908 | measured, final stretch | Good |
| Runner (baseline, 32 scenarios) | Sonnet | about $0.267 | 0.233908 x 32 / 28 = 0.267323 | Rough: assumes cost scales with scenario count |
| Diagnostician, iteration 0 | Opus | about $0.108 | 0.342048 - 0.233908 = 0.108140 | Rough: assumes the round-1 pass cost the same as the final pass |
| Diagnostician + Fixer + Verifier re-run, round 2 | Opus + Sonnet + Sonnet | about $0.354 total; about $0.120 for Diagnostician + Fixer | 0.353852 - 0.233908 = 0.119944 (assuming the Verifier re-run costs one full pass) | Rough: Diagnostician and Fixer cannot be separated, and the Verifier may run fewer scenarios than a full pass |
| Planner + Builder + Agent Smith + Simulator (setup) | Opus, Sonnet, Sonnet, Haiku | about $0.554 together | 0.821175 - 0.267323 = 0.553852 | Rough: four heads in one number; the Simulator part uses the placeholder Haiku rate |

Observation to check, not a conclusion: the Verifier re-runs the scenarios (events 36 to 37) and the Runner then runs them again on the same agent version (events 38 to 40). If both are full passes, that is about $0.23 spent twice per kept fix.

## 5. Time (not a cost, but useful for sandbox sizing)

From `events[].at`: first event 2026-10-11T23:04:42.960Z, last event 23:11:11.879Z = **388.9 s (6.5 min)** between first and last event. (EVIDENCE.md quotes the run as 04:20 to 04:41 IST; the events themselves cover about 04:34 to 04:41 IST, so the earlier time presumably includes startup before the first event.) Longest stretches: Simulator ~181 s (event 18 to 19), Builder ~38 s, one Runner pass 14 to 18 s.

## 6. Inputs to enter in cfo.ai

| Input | Value | Basis |
|---|---|---|
| Anthropic cost per full hero run | **$1.75** | hero-2, $1.750983, n = 1 |
| Of which: setup + baseline | $0.82 | measured |
| Of which: each improvement round | about $0.35 | measured, two rounds |
| Of which: final verification pass | $0.23 | measured |
| Cost of one scenario run by the generated agent (Sonnet) | about $0.0084 | 0.233908 / 28; a proxy for the Anthropic cost of one review by a deployed agent, mocks only |
| Per-run cost cap | $2.00 | `RUN_COST_CAP_USD` default |
| Sandbox compute per run | TODO(Muaaz/Jugaad): not measured | needs an EC2 run |
| Fixed monthly costs | TODO(Jugaad): not measured | hosting, database, domains |

Not covered by the run: the cost of the second hero spec (`specs/backup.md`), runs that need more rounds, and any run through the live site rather than `scripts/run_local.py`. Re-measure once those exist.

## 7. How to recompute

From the saved run:

```bash
# total run cost
jq '.cost_usd' docs/evidence/hero-2-run.json
# running total at each scored pass (iteration, cost)
jq -r '.metrics[] | [.iteration, .cost_usd] | @tsv' docs/evidence/hero-2-run.json
# same totals as they appear in the event log (Runner/Verifier "metrics" events)
jq -c '[.events[] | select(.type=="metrics") | .payload.cost_usd]' docs/evidence/hero-2-run.json
# final stretch = total minus last metrics total
python3 -c "import json;d=json.load(open('docs/evidence/hero-2-run.json'));print(d['cost_usd']-d['metrics'][-1]['cost_usd'])"
# wall-clock seconds between first and last event
python3 -c "import json;from datetime import datetime as D;e=json.load(open('docs/evidence/hero-2-run.json'))['events'];print((D.fromisoformat(e[-1]['at'])-D.fromisoformat(e[0]['at'])).total_seconds())"
```

From the database (`supabase/migrations/001_init.sql`): `runs.cost_usd` holds the run total; `iterations` has no cost column, so per-iteration cost comes from `run_events` rows where `type = 'metrics'` (`payload->>'cost_usd'` is the running total; subtract consecutive rows).

```sql
select seq, head, (payload->>'cost_usd')::numeric as running_total
from public.run_events
where run_id = '<run id>' and type = 'metrics'
order by seq;
```

From neatlogs: open each trace id (28 final-iteration ids are in `results[].trace_id` of the JSON; `jq -r '.results[].trace_id' docs/evidence/hero-2-run.json`), read tokens and cost per span, and group spans by the head name each head wraps its work in. That gives the true per-head split and the token counts that this file could not fill in. TODO(Muaaz): replace section 4 with those figures when you have them.

To re-price after the Haiku rate is confirmed: edit `PRICING_USD_PER_MTOK` in `packages/core/susanoo/integrations/llm.py` for future runs. Saved runs keep the cost they were computed with; recomputing an old run needs its token counts from neatlogs times the new rate per 1M tokens.
