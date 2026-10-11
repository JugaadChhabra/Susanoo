-- Susanoo data model. [PRD > Data model]
-- LangGraph checkpoint tables are NOT created here; PostgresSaver.setup() owns them.
--
-- Access model: signed-in users get read access to their own rows (ownership is
-- projects.user_id, followed down the FK chain), plus full CRUD on their own
-- projects and delete on their own connections. Every other write (runs, events,
-- scenarios, results, connection secrets...) goes through the API / worker, which
-- use the service role and bypass RLS.

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------- tables

create table public.users (
    id         uuid primary key references auth.users (id) on delete cascade,
    email      text not null,
    runs_today int  not null default 0 check (runs_today >= 0)
);

create table public.connections (
    id              uuid primary key default gen_random_uuid(),
    user_id         uuid not null references public.users (id) on delete cascade,
    connector       text not null,
    vault_secret_id text not null,  -- points at vault.secrets; the secret itself is never stored here
    status          text not null default 'untested',
    writes_enabled  boolean not null default false,
    unique (user_id, connector)
);

create table public.projects (
    id       uuid primary key default gen_random_uuid(),
    user_id  uuid not null references public.users (id) on delete cascade,
    name     text not null,
    spec     text not null,
    repo_url text
);

create table public.runs (
    id           uuid primary key default gen_random_uuid(),
    project_id   uuid not null references public.projects (id) on delete cascade,
    status       text not null default 'running'
                 check (status in ('running', 'awaiting_approval', 'done', 'failed')),
    cost_usd     numeric(12, 4) not null default 0 check (cost_usd >= 0),
    cost_cap_usd numeric(12, 4) not null default 2 check (cost_cap_usd >= 0),
    preview_url  text,
    created_at   timestamptz not null default now()
);

create table public.intake_items (
    id                uuid primary key default gen_random_uuid(),
    run_id            uuid not null references public.runs (id) on delete cascade,
    kind              text not null,
    question          text not null,
    suggested_default text,
    answer            text
);

create table public.run_events (
    id     bigint generated always as identity primary key,
    run_id uuid not null references public.runs (id) on delete cascade,
    seq    int  not null,
    head   text not null,
    type   text not null,
    payload jsonb not null default '{}'::jsonb,
    at     timestamptz not null default now()
);

create table public.scenarios (
    id               uuid primary key default gen_random_uuid(),
    run_id           uuid not null references public.runs (id) on delete cascade,
    kind             text not null check (kind in ('planted', 'edge', 'adversarial', 'clean')),
    split            text not null check (split in ('train', 'heldout', 'acceptance')),
    title            text not null,
    diff             text not null,
    answer_key       jsonb not null default '{}'::jsonb,
    approved_by_user boolean not null default false
);

create table public.iterations (
    id                    uuid primary key default gen_random_uuid(),
    run_id                uuid not null references public.runs (id) on delete cascade,
    n                     int  not null check (n >= 0),
    recall                numeric,
    precision             numeric,
    heldout_f1            numeric,
    acceptance_pass_rate  numeric,
    unique (run_id, n)
);

create table public.results (
    id           uuid primary key default gen_random_uuid(),
    iteration_id uuid not null references public.iterations (id) on delete cascade,
    scenario_id  uuid not null references public.scenarios (id) on delete cascade,
    passed       boolean not null,
    findings     jsonb not null default '[]'::jsonb,
    trace_id     text,
    unique (iteration_id, scenario_id)
);

create table public.diagnoses (
    id                uuid primary key default gen_random_uuid(),
    iteration_id      uuid not null references public.iterations (id) on delete cascade,
    layer             text not null check (layer in ('code', 'tool', 'prompt_policy', 'test')),
    hypothesis        text not null,
    evidence_span_ids jsonb not null default '[]'::jsonb
);

create table public.patches (
    id            uuid primary key default gen_random_uuid(),
    diagnosis_id  uuid not null unique references public.diagnoses (id) on delete cascade,  -- 0..1 patch per diagnosis
    commit_sha    text not null,
    checkpoint_id text,  -- Entire
    verdict       text not null default 'pending'
                  check (verdict in ('pending', 'kept', 'rolled_back', 'awaiting_approval'))
);

create table public.approvals (
    id         uuid primary key default gen_random_uuid(),
    patch_id   uuid not null unique references public.patches (id) on delete cascade,  -- 0..1 approval per patch
    decision   text not null,
    decided_by uuid references public.users (id) on delete set null
);

-- ---------------------------------------------------------------- indexes
-- seq is unique per run; the unique index doubles as the (run_id, seq) lookup index.
create unique index run_events_run_id_seq_idx on public.run_events (run_id, seq);
create index runs_status_created_at_idx on public.runs (status, created_at);
-- FK lookups used by RLS ownership checks and the dashboard
create index connections_user_id_idx on public.connections (user_id);
create index projects_user_id_idx on public.projects (user_id);
create index runs_project_id_idx on public.runs (project_id);
create index intake_items_run_id_idx on public.intake_items (run_id);
create index scenarios_run_id_idx on public.scenarios (run_id);
create index iterations_run_id_idx on public.iterations (run_id);
create index results_scenario_id_idx on public.results (scenario_id);
create index diagnoses_iteration_id_idx on public.diagnoses (iteration_id);

-- ---------------------------------------------------------------- signup -> public.users

create function public.handle_new_user() returns trigger
language plpgsql security definer set search_path = '' as $$
begin
    insert into public.users (id, email) values (new.id, coalesce(new.email, ''));
    return new;
end $$;

create trigger on_auth_user_created
    after insert on auth.users
    for each row execute function public.handle_new_user();

-- ---------------------------------------------------------------- Vault
-- Secrets live in vault.secrets (encrypted at rest). Only the service role (API)
-- may call these; users never see or send secret values through PostgREST.

create function public.save_connection(
    p_user_id uuid, p_connector text, p_secret text, p_writes_enabled boolean default false
) returns uuid
language plpgsql security definer set search_path = '' as $$
declare
    v_old text;
    v_secret uuid;
    v_id uuid;
begin
    select vault_secret_id into v_old from public.connections
        where user_id = p_user_id and connector = p_connector;
    v_secret := vault.create_secret(p_secret, 'conn:' || p_user_id || ':' || p_connector || ':' || gen_random_uuid());
    insert into public.connections (user_id, connector, vault_secret_id, writes_enabled)
        values (p_user_id, p_connector, v_secret::text, p_writes_enabled)
        on conflict (user_id, connector) do update
            set vault_secret_id = excluded.vault_secret_id,
                writes_enabled = excluded.writes_enabled,
                status = 'untested'
        returning id into v_id;
    if v_old is not null then
        delete from vault.secrets where id = v_old::uuid;
    end if;
    return v_id;
end $$;

create function public.read_connection_secret(p_connection_id uuid) returns text
language plpgsql security definer set search_path = '' as $$
declare
    v_secret text;
begin
    select ds.decrypted_secret into v_secret
    from public.connections c
    join vault.decrypted_secrets ds on ds.id = c.vault_secret_id::uuid
    where c.id = p_connection_id;
    return v_secret;
end $$;

-- Revoking a connection removes its secret too.
create function public.delete_connection_secret() returns trigger
language plpgsql security definer set search_path = '' as $$
begin
    delete from vault.secrets where id = old.vault_secret_id::uuid;
    return old;
end $$;

create trigger connections_delete_secret
    after delete on public.connections
    for each row execute function public.delete_connection_secret();

revoke all on function public.save_connection(uuid, text, text, boolean) from public, anon, authenticated;
revoke all on function public.read_connection_secret(uuid) from public, anon, authenticated;
revoke all on function public.delete_connection_secret() from public, anon, authenticated;
revoke all on function public.handle_new_user() from public, anon, authenticated;
grant execute on function public.save_connection(uuid, text, text, boolean) to service_role;
grant execute on function public.read_connection_secret(uuid) to service_role;

-- ---------------------------------------------------------------- row-level security

alter table public.users        enable row level security;
alter table public.connections  enable row level security;
alter table public.projects     enable row level security;
alter table public.runs         enable row level security;
alter table public.intake_items enable row level security;
alter table public.run_events   enable row level security;
alter table public.scenarios    enable row level security;
alter table public.iterations   enable row level security;
alter table public.results      enable row level security;
alter table public.diagnoses    enable row level security;
alter table public.patches      enable row level security;
alter table public.approvals    enable row level security;

-- Start from nothing, then grant the minimum back (column-level where a column must stay server-side).
revoke all on all tables in schema public from anon, authenticated;
-- Tables added by later migrations start with no client access too.
alter default privileges in schema public revoke all on tables from anon, authenticated;
alter default privileges in schema public revoke all on sequences from anon, authenticated;
alter default privileges in schema public revoke all on functions from anon, authenticated;

grant select on public.users, public.runs, public.intake_items, public.run_events, public.scenarios,
    public.iterations, public.results, public.diagnoses, public.patches, public.approvals to authenticated;
-- vault_secret_id is never exposed to clients
grant select (id, user_id, connector, status, writes_enabled) on public.connections to authenticated;
grant delete on public.connections to authenticated;
-- repo_url is set by the worker only
grant select, delete on public.projects to authenticated;
grant insert (user_id, name, spec), update (name, spec) on public.projects to authenticated;

create policy users_select_own on public.users for select to authenticated
    using (id = (select auth.uid()));

create policy connections_select_own on public.connections for select to authenticated
    using (user_id = (select auth.uid()));
create policy connections_delete_own on public.connections for delete to authenticated
    using (user_id = (select auth.uid()));

create policy projects_select_own on public.projects for select to authenticated
    using (user_id = (select auth.uid()));
create policy projects_insert_own on public.projects for insert to authenticated
    with check (user_id = (select auth.uid()));
create policy projects_update_own on public.projects for update to authenticated
    using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()));
create policy projects_delete_own on public.projects for delete to authenticated
    using (user_id = (select auth.uid()));

-- Everything below is owned through projects.user_id.
create policy runs_select_own on public.runs for select to authenticated
    using (exists (select 1 from public.projects p where p.id = runs.project_id and p.user_id = (select auth.uid())));

create policy intake_items_select_own on public.intake_items for select to authenticated
    using (exists (select 1 from public.runs r join public.projects p on p.id = r.project_id
                   where r.id = intake_items.run_id and p.user_id = (select auth.uid())));

create policy run_events_select_own on public.run_events for select to authenticated
    using (exists (select 1 from public.runs r join public.projects p on p.id = r.project_id
                   where r.id = run_events.run_id and p.user_id = (select auth.uid())));

create policy scenarios_select_own on public.scenarios for select to authenticated
    using (exists (select 1 from public.runs r join public.projects p on p.id = r.project_id
                   where r.id = scenarios.run_id and p.user_id = (select auth.uid())));

create policy iterations_select_own on public.iterations for select to authenticated
    using (exists (select 1 from public.runs r join public.projects p on p.id = r.project_id
                   where r.id = iterations.run_id and p.user_id = (select auth.uid())));

create policy results_select_own on public.results for select to authenticated
    using (exists (select 1 from public.iterations i join public.runs r on r.id = i.run_id
                   join public.projects p on p.id = r.project_id
                   where i.id = results.iteration_id and p.user_id = (select auth.uid())));

create policy diagnoses_select_own on public.diagnoses for select to authenticated
    using (exists (select 1 from public.iterations i join public.runs r on r.id = i.run_id
                   join public.projects p on p.id = r.project_id
                   where i.id = diagnoses.iteration_id and p.user_id = (select auth.uid())));

create policy patches_select_own on public.patches for select to authenticated
    using (exists (select 1 from public.diagnoses d join public.iterations i on i.id = d.iteration_id
                   join public.runs r on r.id = i.run_id join public.projects p on p.id = r.project_id
                   where d.id = patches.diagnosis_id and p.user_id = (select auth.uid())));

create policy approvals_select_own on public.approvals for select to authenticated
    using (exists (select 1 from public.patches pa join public.diagnoses d on d.id = pa.diagnosis_id
                   join public.iterations i on i.id = d.iteration_id
                   join public.runs r on r.id = i.run_id join public.projects p on p.id = r.project_id
                   where pa.id = approvals.patch_id and p.user_id = (select auth.uid())));
