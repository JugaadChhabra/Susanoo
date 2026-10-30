create table platform.resource_leases (
    id uuid primary key default gen_random_uuid(),
    workspace_id uuid not null,
    project_id uuid not null,
    attempt_id uuid not null,
    resource_key text not null,
    fencing_generation bigint not null,
    state text not null check(state in ('leased','quarantined','released')),
    created_at timestamptz not null default now(),
    released_at timestamptz,
    foreign key(workspace_id,project_id,attempt_id) references platform.attempts(workspace_id,project_id,id)
);
create unique index resource_exclusive_live on platform.resource_leases(workspace_id,resource_key)
    where state<>'released';
alter table platform.resource_leases enable row level security;
revoke all on platform.resource_leases from public;
do $$ begin
    if exists(select 1 from pg_roles where rolname='service_role') then
        grant all on platform.resource_leases to service_role;
    end if;
end $$;
insert into platform.schema_versions(version) values('20261030093254');
