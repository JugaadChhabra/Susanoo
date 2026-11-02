alter table platform.credentials drop constraint credentials_kind_check;
alter table platform.credentials add constraint credentials_kind_check
    check(kind in ('session','cli','registration','runner','invitation','password_reset','collector'));
alter table platform.schedules add column automatic_budget jsonb not null default '{}';
create table platform.automatic_decisions (
    id uuid primary key default gen_random_uuid(),
    workspace_id uuid not null, project_id uuid not null,
    trigger_key text not null, trigger_kind text not null,
    version_key text not null, suite_version_id uuid not null,
    allowed boolean not null, reasons jsonb not null, delayed_until timestamptz,
    secrets_allowed boolean not null, run_id uuid references platform.runs(id),
    created_at timestamptz not null default now(),
    unique(project_id,trigger_kind,trigger_key,version_key,suite_version_id),
    foreign key(workspace_id,project_id,suite_version_id) references platform.versions(workspace_id,project_id,id),
    foreign key(workspace_id,project_id) references platform.projects(workspace_id,id)
);
create index automatic_decisions_window on platform.automatic_decisions(project_id,created_at);
create table platform.relay_tasks (
    id uuid primary key default gen_random_uuid(),
    workspace_id uuid not null, project_id uuid not null, attempt_id uuid not null,
    integration_id uuid not null, task_id text not null, invocation_id text not null, operation_id text not null,
    last_sequence bigint not null default -1, terminal boolean not null default false,
    expires_at timestamptz not null, created_at timestamptz not null default now(),
    unique(integration_id,task_id), unique(workspace_id,project_id,id),
    foreign key(workspace_id,project_id,attempt_id) references platform.attempts(workspace_id,project_id,id),
    foreign key(workspace_id,project_id,integration_id) references platform.integrations(workspace_id,project_id,id)
);
create table platform.relay_events (
    cursor bigint generated always as identity primary key,
    workspace_id uuid not null, project_id uuid not null, task_id uuid not null,
    event_id text not null, payload_hash text not null, payload jsonb not null,
    disposition text not null check(disposition in ('accepted','out_of_order','after_terminal')),
    received_at timestamptz not null default now(),
    unique(task_id,event_id),
    foreign key(workspace_id,project_id,task_id) references platform.relay_tasks(workspace_id,project_id,id)
);
create table platform.passive_records (
    workspace_id uuid not null, project_id uuid not null,
    record_id text not null, payload jsonb not null, created_at timestamptz not null default now(),
    primary key(project_id,record_id),
    foreign key(workspace_id,project_id) references platform.projects(workspace_id,id)
);
create table platform.advanced_reports (
    id uuid primary key default gen_random_uuid(),
    workspace_id uuid not null, project_id uuid not null, run_id uuid not null, attempt_id uuid not null,
    report_type text not null, report_key text not null, content_hash text not null, payload jsonb not null,
    policy_hash text not null,
    hidden boolean not null, audience text not null check(audience in ('project','reviewers')),
    created_at timestamptz not null default now(),
    unique(attempt_id,report_type,report_key), unique(workspace_id,project_id,id),
    foreign key(workspace_id,project_id,attempt_id) references platform.attempts(workspace_id,project_id,id),
    foreign key(workspace_id,project_id,run_id) references platform.runs(workspace_id,project_id,id)
);
create table platform.report_reviews (
    id uuid primary key default gen_random_uuid(),
    workspace_id uuid not null, project_id uuid not null, report_id uuid not null,
    actor_id uuid not null references platform.identities(id),
    candidate_id text not null, classification text not null, rationale text not null, evidence_ids uuid[] not null,
    created_at timestamptz not null default now(),
    foreign key(workspace_id,project_id,report_id) references platform.advanced_reports(workspace_id,project_id,id)
);
do $$ declare t text; begin
    foreach t in array array['automatic_decisions','relay_tasks','relay_events','passive_records',
        'advanced_reports','report_reviews'] loop
        execute format('alter table platform.%I enable row level security',t);
        execute format('revoke all on platform.%I from public',t);
    end loop;
    foreach t in array array['relay_events','passive_records','advanced_reports','report_reviews'] loop
        execute format('create trigger immutable_update before update on platform.%I
            for each row execute function platform.immutable_row()',t);
    end loop;
end $$;
insert into platform.schema_versions(version) values('20261102020000');
