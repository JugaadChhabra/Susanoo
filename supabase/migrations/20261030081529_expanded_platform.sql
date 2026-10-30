-- Additive expanded control plane. No legacy public tables or auth data are changed.
create schema if not exists platform;
revoke all on schema platform from public;

create table platform.schema_versions (
    version text primary key,
    applied_at timestamptz not null default now()
);
create table platform.identities (
    id uuid primary key,
    email text not null unique,
    password_hash text,
    disabled boolean not null default false,
    created_at timestamptz not null default now()
);
create table platform.workspaces (
    id uuid primary key default gen_random_uuid(),
    name text not null,
    deleted_at timestamptz,
    purge_after timestamptz,
    created_at timestamptz not null default now()
);
create table platform.memberships (
    workspace_id uuid not null references platform.workspaces(id),
    user_id uuid not null references platform.identities(id),
    roles text[] not null check (cardinality(roles)>0 and roles <@ array['owner','engineer','reviewer','viewer']),
    primary key(workspace_id,user_id)
);
create table platform.projects (
    id uuid primary key default gen_random_uuid(),
    workspace_id uuid not null references platform.workspaces(id),
    name text not null,
    workflow text not null,
    timezone text not null,
    policy jsonb not null,
    deleted_at timestamptz,
    purge_after timestamptz,
    created_at timestamptz not null default now(),
    unique(workspace_id,id)
);
create table platform.project_grants (
    workspace_id uuid not null,
    project_id uuid not null,
    user_id uuid not null,
    roles text[] not null check (cardinality(roles)>0 and roles <@ array['owner','engineer','reviewer','viewer']),
    hidden_access boolean not null default false,
    primary key(project_id,user_id),
    foreign key(workspace_id,project_id) references platform.projects(workspace_id,id),
    foreign key(workspace_id,user_id) references platform.memberships(workspace_id,user_id)
);
create table platform.credentials (
    id uuid primary key default gen_random_uuid(),
    token_hash text not null unique,
    kind text not null check(kind in ('session','cli','registration','runner','invitation','password_reset')),
    user_id uuid references platform.identities(id),
    workspace_id uuid references platform.workspaces(id),
    project_id uuid,
    runner_id uuid,
    metadata jsonb not null default '{}',
    expires_at timestamptz not null,
    revoked_at timestamptz,
    consumed_at timestamptz,
    created_at timestamptz not null default now(),
    foreign key(workspace_id,project_id) references platform.projects(workspace_id,id)
);
create table platform.versions (
    id uuid primary key default gen_random_uuid(),
    workspace_id uuid not null,
    project_id uuid not null,
    kind text not null,
    logical_id text not null,
    revision integer not null check(revision>0),
    title text not null,
    data jsonb not null check(data->>'kind'=kind),
    provenance jsonb not null default '{}',
    content_hash text not null check(content_hash ~ '^[a-f0-9]{64}$'),
    hidden boolean not null default false,
    created_by uuid not null references platform.identities(id),
    created_at timestamptz not null default now(),
    unique(project_id,kind,logical_id,revision),
    unique(workspace_id,project_id,id),
    foreign key(workspace_id,project_id) references platform.projects(workspace_id,id)
);
create table platform.version_links (
    workspace_id uuid not null,
    project_id uuid not null,
    source_id uuid not null,
    target_id uuid not null,
    relationship text not null,
    primary key(source_id,target_id,relationship),
    foreign key(workspace_id,project_id,source_id) references platform.versions(workspace_id,project_id,id),
    foreign key(workspace_id,project_id,target_id) references platform.versions(workspace_id,project_id,id),
    check(source_id<>target_id)
);
create table platform.approvals (
    id uuid primary key default gen_random_uuid(),
    workspace_id uuid not null,
    project_id uuid not null,
    version_id uuid not null,
    content_hash text not null,
    actor_id uuid not null references platform.identities(id),
    rationale text not null,
    created_at timestamptz not null default now(),
    unique(version_id,content_hash),
    foreign key(workspace_id,project_id,version_id) references platform.versions(workspace_id,project_id,id)
);
create table platform.runners (
    id uuid primary key default gen_random_uuid(),
    workspace_id uuid not null,
    project_id uuid not null,
    name text not null,
    version text not null,
    protocol_version text not null,
    capabilities text[] not null,
    last_heartbeat timestamptz,
    revoked_at timestamptz,
    unique(workspace_id,project_id,id),
    foreign key(workspace_id,project_id) references platform.projects(workspace_id,id)
);
alter table platform.credentials add foreign key(workspace_id,project_id,runner_id)
    references platform.runners(workspace_id,project_id,id);
create table platform.runs (
    id uuid primary key default gen_random_uuid(),
    workspace_id uuid not null,
    project_id uuid not null,
    kind text not null,
    manifest jsonb not null,
    manifest_hash text not null,
    request_hash text not null,
    idempotency_key text not null,
    operation text not null,
    status text not null default 'queued',
    gate text not null default 'IN_PROGRESS',
    created_by uuid not null references platform.identities(id),
    cancel_requested_at timestamptz,
    original_run_id uuid,
    created_at timestamptz not null default now(),
    unique(workspace_id,operation,idempotency_key),
    unique(workspace_id,project_id,id),
    foreign key(workspace_id,project_id) references platform.projects(workspace_id,id),
    foreign key(workspace_id,project_id,original_run_id) references platform.runs(workspace_id,project_id,id)
);
create table platform.attempts (
    id uuid primary key default gen_random_uuid(),
    workspace_id uuid not null,
    project_id uuid not null,
    run_id uuid not null,
    case_version_id uuid,
    repetition integer not null default 0 check(repetition>=0),
    arm text not null default 'primary',
    namespace text not null unique,
    required boolean not null default true,
    hidden boolean not null default false,
    execution_state text not null default 'queued',
    business_verdict text,
    runner_id uuid,
    lease_hash text,
    lease_expires_at timestamptz,
    fencing_generation bigint not null default 0,
    started_at timestamptz,
    deadline timestamptz,
    cancel_acknowledged_at timestamptz,
    cleanup_state text not null default 'pending',
    reconciliation_state text not null default 'not_needed',
    finalized_hash text,
    final_sequence integer,
    reason text not null default '',
    unique(run_id,case_version_id,repetition,arm),
    unique(workspace_id,project_id,id),
    foreign key(workspace_id,project_id,run_id) references platform.runs(workspace_id,project_id,id),
    foreign key(workspace_id,project_id,case_version_id) references platform.versions(workspace_id,project_id,id),
    foreign key(workspace_id,project_id,runner_id) references platform.runners(workspace_id,project_id,id)
);
create table platform.runner_events (
    ingestion_seq bigint generated always as identity unique,
    workspace_id uuid not null,
    project_id uuid not null,
    attempt_id uuid not null,
    event_id uuid not null,
    sequence integer not null check(sequence>0),
    event_type text not null,
    payload jsonb not null,
    payload_hash text not null,
    occurred_at timestamptz not null,
    received_at timestamptz not null default now(),
    primary key(attempt_id,event_id),
    unique(attempt_id,sequence),
    foreign key(workspace_id,project_id,attempt_id) references platform.attempts(workspace_id,project_id,id)
);
create table platform.evidence (
    id uuid primary key,
    workspace_id uuid not null,
    project_id uuid not null,
    attempt_id uuid not null,
    kind text not null check(kind in ('observation','check')),
    version_id uuid,
    data jsonb not null,
    content_hash text not null,
    hidden boolean not null default false,
    received_at timestamptz not null default now(),
    unique(attempt_id,kind,version_id),
    foreign key(workspace_id,project_id,attempt_id) references platform.attempts(workspace_id,project_id,id),
    foreign key(workspace_id,project_id,version_id) references platform.versions(workspace_id,project_id,id)
);
create table platform.findings (
    id uuid primary key default gen_random_uuid(),
    workspace_id uuid not null,
    project_id uuid not null,
    run_id uuid not null,
    attempt_id uuid not null,
    check_version_id uuid not null,
    verdict text not null,
    hidden boolean not null default false,
    unique(attempt_id,check_version_id),
    unique(workspace_id,project_id,id),
    foreign key(workspace_id,project_id,run_id) references platform.runs(workspace_id,project_id,id),
    foreign key(workspace_id,project_id,attempt_id) references platform.attempts(workspace_id,project_id,id),
    foreign key(workspace_id,project_id,check_version_id) references platform.versions(workspace_id,project_id,id)
);
create table platform.review_events (
    id uuid primary key default gen_random_uuid(),
    workspace_id uuid not null,
    project_id uuid not null,
    subject_id uuid not null,
    actor_id uuid not null references platform.identities(id),
    decision text not null,
    rationale text not null,
    evidence_ids uuid[] not null default '{}',
    expires_at timestamptz,
    metadata jsonb not null default '{}',
    created_at timestamptz not null default now(),
    foreign key(workspace_id,project_id) references platform.projects(workspace_id,id)
);
create table platform.artifacts (
    id uuid primary key default gen_random_uuid(),
    workspace_id uuid not null,
    project_id uuid not null,
    run_id uuid,
    export_format text,
    content_type text not null,
    digest text not null,
    size bigint not null check(size>=0),
    object_key text not null unique,
    audience text not null check(audience in ('project','reviewers')),
    hidden boolean not null default false,
    status text not null default 'pending',
    snapshot jsonb,
    created_by uuid not null references platform.identities(id),
    created_at timestamptz not null default now(),
    expires_at timestamptz not null,
    foreign key(workspace_id,project_id) references platform.projects(workspace_id,id),
    foreign key(workspace_id,project_id,run_id) references platform.runs(workspace_id,project_id,id)
);
create table platform.jobs (
    id uuid primary key default gen_random_uuid(),
    workspace_id uuid not null,
    project_id uuid not null,
    kind text not null,
    payload jsonb not null,
    dedupe_key text not null,
    status text not null default 'queued',
    available_at timestamptz not null default now(),
    claimed_by uuid,
    lease_expires_at timestamptz,
    attempts integer not null default 0 check(attempts>=0),
    error_code text,
    unique(project_id,kind,dedupe_key),
    foreign key(workspace_id,project_id) references platform.projects(workspace_id,id)
);
create table platform.schedules (
    id uuid primary key default gen_random_uuid(),
    workspace_id uuid not null,
    project_id uuid not null,
    name text not null,
    interval_seconds integer not null check(interval_seconds>=60),
    configuration jsonb not null,
    enabled boolean not null default false,
    next_due_at timestamptz not null,
    created_by uuid not null references platform.identities(id),
    foreign key(workspace_id,project_id) references platform.projects(workspace_id,id)
);
create table platform.invalidations (
    id uuid primary key default gen_random_uuid(),
    workspace_id uuid not null,
    project_id uuid not null,
    version_id uuid not null,
    change_id uuid not null,
    classification text not null,
    reason text not null,
    dependency_path uuid[] not null,
    created_at timestamptz not null default now(),
    unique(version_id,change_id),
    foreign key(workspace_id,project_id,version_id) references platform.versions(workspace_id,project_id,id)
);
create table platform.integrations (
    id uuid primary key default gen_random_uuid(),
    workspace_id uuid not null,
    project_id uuid not null,
    provider text not null,
    installation_id bigint,
    repository text,
    secret_ref text not null,
    configuration jsonb not null default '{}',
    revoked_at timestamptz,
    unique(workspace_id,project_id,id),
    foreign key(workspace_id,project_id) references platform.projects(workspace_id,id)
);
create table platform.deliveries (
    workspace_id uuid not null,
    project_id uuid not null,
    integration_id uuid not null,
    delivery_id text not null,
    payload_hash text not null,
    payload jsonb not null,
    created_at timestamptz not null default now(),
    primary key(integration_id,delivery_id),
    foreign key(workspace_id,project_id,integration_id) references platform.integrations(workspace_id,project_id,id)
);
create table platform.audit (
    id bigint generated always as identity primary key,
    workspace_id uuid,
    project_id uuid,
    actor_id uuid,
    action text not null,
    subject_id uuid,
    correlation_id uuid not null,
    details jsonb not null default '{}',
    created_at timestamptz not null default now()
);
create table platform.deletion_tombstones (
    project_id uuid primary key,
    workspace_id uuid not null,
    deleted_at timestamptz not null,
    purge_deadline timestamptz not null,
    purged_at timestamptz
);
create table platform.repairs (
    id uuid primary key default gen_random_uuid(),
    workspace_id uuid not null,
    project_id uuid not null,
    finding_id uuid not null,
    source_version_id uuid not null,
    patch_artifact_id uuid not null references platform.artifacts(id),
    layer text not null check(layer in ('agent','simulator','evaluator')),
    base_commit text not null,
    proposal_hash text not null,
    validation_run_id uuid,
    approved_by uuid references platform.identities(id),
    approved_at timestamptz,
    state text not null default 'draft',
    publication_key text not null unique,
    pr_url text,
    created_by uuid not null references platform.identities(id),
    created_at timestamptz not null default now(),
    foreign key(workspace_id,project_id) references platform.projects(workspace_id,id),
    foreign key(workspace_id,project_id,finding_id) references platform.findings(workspace_id,project_id,id),
    foreign key(workspace_id,project_id,source_version_id) references platform.versions(workspace_id,project_id,id),
    foreign key(workspace_id,project_id,validation_run_id) references platform.runs(workspace_id,project_id,id)
);
create table platform.mail_outbox (
    id uuid primary key default gen_random_uuid(),
    recipient text not null,
    subject text not null,
    body text not null,
    credential_id uuid not null references platform.credentials(id),
    status text not null default 'queued',
    created_at timestamptz not null default now()
);
create table platform.login_attempts (
    key_hash text primary key,
    failures integer not null default 0,
    window_start timestamptz not null default now()
);
create table platform.budget_reservations (
    workspace_id uuid not null,
    project_id uuid not null,
    run_id uuid not null,
    operation_id text not null,
    budget_kind text not null check(budget_kind in ('agent','reference')),
    amount integer not null check(amount>0),
    created_at timestamptz not null default now(),
    primary key(run_id,operation_id),
    foreign key(workspace_id,project_id,run_id) references platform.runs(workspace_id,project_id,id)
);

create index on platform.versions(project_id,kind,created_at,id);
create index on platform.runs(project_id,created_at,id);
create index on platform.attempts(run_id,execution_state);
create index on platform.attempts(execution_state,lease_expires_at);
create index on platform.evidence(attempt_id,kind);
create index on platform.version_links(target_id);
create index on platform.jobs(status,available_at);
create index on platform.artifacts(expires_at);
create index on platform.audit(workspace_id,project_id,id);

create function platform.immutable_row() returns trigger language plpgsql as $$
begin
    raise exception 'immutable evidence/version: append a new revision' using errcode='23514';
end $$;
do $$ declare t text; begin
    foreach t in array array['versions','version_links','approvals','runner_events','evidence','findings','review_events','audit'] loop
        execute format('create trigger immutable_update before update on platform.%I for each row execute function platform.immutable_row()',t);
    end loop;
    for t in select tablename from pg_tables where schemaname='platform' loop
        execute format('alter table platform.%I enable row level security',t);
        execute format('revoke all on platform.%I from public',t);
    end loop;
    if exists(select 1 from pg_roles where rolname='service_role') then
        grant usage on schema platform to service_role;
        grant all on all tables in schema platform to service_role;
        grant all on all sequences in schema platform to service_role;
    end if;
end $$;
revoke all on all functions in schema platform from public;
insert into platform.schema_versions(version) values('20261030081529');
