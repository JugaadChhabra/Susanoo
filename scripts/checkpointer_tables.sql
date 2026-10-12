-- LangGraph checkpoint tables, exactly what PostgresSaver.setup() runs (langgraph-checkpoint-postgres 3.1.2).
-- Paste into the Supabase SQL Editor and run once. Safe to re-run (if not exists).
-- CONCURRENTLY is dropped because the SQL Editor runs in a transaction; the tables are empty, so it doesn't matter.
-- The last statements record the applied versions so setup() later sees nothing left to do.

-- migration 0
CREATE TABLE IF NOT EXISTS checkpoint_migrations (
    v INTEGER PRIMARY KEY
);

-- migration 1
CREATE TABLE IF NOT EXISTS checkpoints (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    parent_checkpoint_id TEXT,
    type TEXT,
    checkpoint JSONB NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}',
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);

-- migration 2
CREATE TABLE IF NOT EXISTS checkpoint_blobs (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL,
    version TEXT NOT NULL,
    type TEXT NOT NULL,
    blob BYTEA,
    PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
);

-- migration 3
CREATE TABLE IF NOT EXISTS checkpoint_writes (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    channel TEXT NOT NULL,
    type TEXT,
    blob BYTEA NOT NULL,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);

-- migration 4
ALTER TABLE checkpoint_blobs ALTER COLUMN blob DROP not null;

-- migration 5
SELECT 1;

-- migration 6
CREATE INDEX IF NOT EXISTS checkpoints_thread_id_idx ON checkpoints(thread_id);

-- migration 7
CREATE INDEX IF NOT EXISTS checkpoint_blobs_thread_id_idx ON checkpoint_blobs(thread_id);

-- migration 8
CREATE INDEX IF NOT EXISTS checkpoint_writes_thread_id_idx ON checkpoint_writes(thread_id);

-- migration 9
ALTER TABLE checkpoint_writes ADD COLUMN IF NOT EXISTS task_path TEXT NOT NULL DEFAULT '';

insert into checkpoint_migrations (v) values (0), (1), (2), (3), (4), (5), (6), (7), (8), (9) on conflict (v) do nothing;

-- Supabase exposes the public schema over its API, so lock these down: no policies means only the table
-- owner (the worker's postgres connection) and the service role can use them; the web client never can.
alter table checkpoint_migrations enable row level security;
alter table checkpoints enable row level security;
alter table checkpoint_blobs enable row level security;
alter table checkpoint_writes enable row level security;
do $$ begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on checkpoint_migrations, checkpoints, checkpoint_blobs, checkpoint_writes from anon, authenticated;
  end if;
end $$;
