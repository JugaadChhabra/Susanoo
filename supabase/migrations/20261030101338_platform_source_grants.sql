create table platform.source_grants (
    id uuid primary key default gen_random_uuid(),
    workspace_id uuid not null,
    project_id uuid not null,
    attempt_id uuid not null,
    repository text not null,
    token_cipher text not null,
    expires_at timestamptz not null,
    state text not null default 'active' check(state in ('active','revoking','revoked','failed')),
    revoke_attempts integer not null default 0,
    claimed_at timestamptz,
    foreign key(workspace_id,project_id,attempt_id) references platform.attempts(workspace_id,project_id,id)
);
alter table platform.source_grants enable row level security;
revoke all on platform.source_grants from public;
create index on platform.source_grants(state,expires_at);
do $$ begin
    if exists(select 1 from pg_roles where rolname='service_role') then
        grant all on platform.source_grants to service_role;
    end if;
end $$;
insert into platform.schema_versions(version) values('20261030101338');
