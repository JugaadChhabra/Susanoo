-- Make public.runs a work queue for the worker (proposed by Muaaz in docs/proposals/006_run_queue.sql).
-- [PRD > Tech stack] (job queue = Postgres table, polled by the worker; no Redis)
-- [PRD > LangGraph flow and shared state] (a crashed run resumes from its checkpoint)
--
-- Additive and idempotent.
--
-- Operations: the worker's DATABASE_URL must be a direct or session-mode connection (port 5432), NOT Supabase's
-- transaction pooler (port 6543): startup uses a session advisory lock and LangGraph's setup() runs
-- CREATE INDEX CONCURRENTLY. The worker refuses to start on port 6543.
--
-- How the pieces fit
--   enqueue  API inserts a run with status 'queued' (the default below).
--   claim    Worker: SELECT ... FOR UPDATE SKIP LOCKED on a 'queued' run, then in the same short
--            transaction set status 'running', claimed_by, claimed_at, heartbeat_at, attempts + 1.
--   beat     The worker bumps heartbeat_at every few seconds (only while claimed_by = itself).
--   crash    A 'running' run whose heartbeat is older than the stale window is claimable again; the worker
--            resumes it from its LangGraph checkpoint (thread_id = run id). It never restarts from scratch.
--   pause    The graph stops at an interrupt: worker sets status 'awaiting_approval', releases the claim and
--            records paused_checkpoint_id (the checkpoint the pause belongs to). Nobody claims it.
--   answer   A pause is identified by a token (= paused_checkpoint_id, written by the worker at the pause).
--            The API returns the token to the browser with the question (worker side: store.pending_pause), the
--            browser echoes it with the answer, and the API (after validating the answer with
--            susanoo.graph.resume.validate_resume) runs, with the token MANDATORY:
--              update public.runs set status = 'queued', resume_value = $answer
--               where id = $run and status = 'awaiting_approval' and paused_checkpoint_id = $token;
--            Zero rows updated means the answer is for an earlier step (or the run moved on): the API answers
--            HTTP 409 with a plain-English message (store.store_resume_value returns it) and changes nothing.
--            This is what stops a stale tab's "approve" for one test-change gate from approving the next gate.
--            The worker re-checks the token against the live checkpoint before applying (backstop for a crash).
--   cancel   The API sets cancel_requested = true. For a run nobody is executing ('queued' or
--            'awaiting_approval') it may also set status = 'cancelled' directly (the worker sweeps these too).
--            For a running run the worker sees the flag within one heartbeat, stops the graph and sets
--            status 'cancelled' with a plain-English event.
--   fail     Any non-budget exception: status 'failed', error = plain-English reason, plus an error event.

-- 1. Statuses: add 'queued' and 'cancelled'. The constraint was created inline in 001, so it is named runs_status_check.
alter table public.runs drop constraint if exists runs_status_check;
alter table public.runs
    add constraint runs_status_check
    check (status in ('queued', 'running', 'awaiting_approval', 'done', 'failed', 'cancelled'));

-- New runs wait in the queue until a worker claims them.
-- (The worker also reclaims a 'running' run with no heartbeat after the stale window, so a run inserted
--  with the old default 'running' is still picked up, just slower.)
alter table public.runs alter column status set default 'queued';

-- 2. Queue bookkeeping (all server-side; no client grants, and the 001 revoke for anon/authenticated already applies).
alter table public.runs
    add column if not exists cancel_requested     boolean     not null default false,
    add column if not exists claimed_by           text,
    add column if not exists claimed_at           timestamptz,
    add column if not exists heartbeat_at         timestamptz,
    add column if not exists attempts             int         not null default 0 check (attempts >= 0),
    add column if not exists error                text,
    add column if not exists resume_value         jsonb,
    add column if not exists paused_checkpoint_id text;

-- 3. The poll: queued runs oldest first, and running runs by heartbeat (stale reclaim).
create index if not exists runs_queue_idx
    on public.runs (created_at)
    where status in ('queued', 'running');
