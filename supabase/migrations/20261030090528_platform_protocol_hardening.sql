alter table platform.versions add constraint version_kind_present
    check(data ? 'kind' and data->>'kind'=kind);
alter table platform.artifacts add column attempt_id uuid;
alter table platform.artifacts add column uploaded_by_runner uuid;
alter table platform.artifacts add foreign key(workspace_id,project_id,attempt_id)
    references platform.attempts(workspace_id,project_id,id);
alter table platform.artifacts add foreign key(workspace_id,project_id,uploaded_by_runner)
    references platform.runners(workspace_id,project_id,id);
alter table platform.budget_reservations add column attempt_id uuid;
alter table platform.budget_reservations add foreign key(workspace_id,project_id,attempt_id)
    references platform.attempts(workspace_id,project_id,id);
alter table platform.integrations add column created_by uuid references platform.identities(id);
alter table platform.deliveries add column ingestion_seq bigint generated always as identity unique;
alter table platform.reconciliations add column actor_kind text not null default 'reviewer'
    check(actor_kind in ('reviewer','runner'));
create table platform.retention_receipts (
    workspace_id uuid not null,
    project_id uuid not null,
    run_id uuid primary key,
    purged_at timestamptz not null default now(),
    reason text not null,
    foreign key(workspace_id,project_id,run_id) references platform.runs(workspace_id,project_id,id)
);
alter table platform.retention_receipts enable row level security;
revoke all on platform.retention_receipts from public;
create trigger immutable_update before update on platform.retention_receipts
    for each row execute function platform.immutable_row();
do $$ begin
    if exists(select 1 from pg_roles where rolname='service_role') then
        grant all on all tables in schema platform to service_role;
        grant usage,select on all sequences in schema platform to service_role;
    end if;
end $$;
insert into platform.schema_versions(version) values('20261030090528');
