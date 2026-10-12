-- A run remembers the connector names (e.g. 'github') its project had when it started, so the worker can put
-- them in the run's state for Agent Smith and the Runner. [PRD > Connectors and API budget]
-- Names only: never connection ids or secrets. Filled by the API at run creation; revoked connections are left out.
alter table public.runs
    add column connectors text[] not null default '{}';

-- Reads are covered by the existing owner-only select on runs. Only the API and worker (service role) write it.
