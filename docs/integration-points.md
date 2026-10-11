# Integration points (Muaaz ↔ Jugaad)

Filled in during Phase 0–1. These are the agreements from §5 of both IMPLEMENTATION files; the contracts themselves are in [PRD › Contracts (locked)].

| Point | Owner | Agreement |
|---|---|---|
| Contracts: `from susanoo.schemas import RunEvent, Scenario, SusanooState, …` (`core.py`, `events.py`, `state.py`, verbatim from the PRD) | both | Landed by Jugaad, Sun 11 Oct; Muaaz to confirm. Fixtures and seed scenarios validate against them. |
| `emit_event()` import path (usable from `packages/core` without importing `apps/api`) | Jugaad | _TBD_ |
| Docker container labels for Caddy `run-<id>` routing | Jugaad (host) / Muaaz (`sandbox/docker.py`) | _TBD_ |
| Tables created by LangGraph `PostgresSaver.setup()` | Muaaz | _TBD_ |
| Fixture files for the mock event stream (`tests/fixtures/run_events.json`, `iteration_metrics.json`) | Muaaz, by Sun 23:00 IST | _TBD_ |
| Connector `tools()` interface used by Agent Smith | Jugaad | See [PRD › Connectors and API budget › Connector contract] |
| Saved demo `run_id` for `/demo` and the marketing proof section | Muaaz | _TBD_ |
