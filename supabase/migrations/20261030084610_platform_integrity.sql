alter table platform.artifacts add unique(workspace_id,project_id,id);
alter table platform.repairs add foreign key(workspace_id,project_id,patch_artifact_id)
    references platform.artifacts(workspace_id,project_id,id);
alter table platform.projects add column policy_version_id uuid;
alter table platform.projects add foreign key(workspace_id,id,policy_version_id)
    references platform.versions(workspace_id,project_id,id);
alter table platform.artifacts add column policy_hash text not null default '';
alter table platform.mail_outbox add column claimed_at timestamptz;
alter table platform.mail_outbox add column attempts integer not null default 0;
alter table platform.attempts add constraint attempt_execution_state check(execution_state in (
    'queued','preparing','running','observing','evaluating','completed','error','timed_out','cancelled','interrupted'));
alter table platform.attempts add constraint attempt_business_verdict check(business_verdict in ('PASS','FAIL','UNKNOWN'));
alter table platform.attempts add constraint attempt_cleanup_state check(cleanup_state in ('pending','succeeded','failed','not_required'));
alter table platform.attempts add constraint attempt_reconciliation_state check(reconciliation_state in ('not_needed','pending','resolved','unresolved'));
alter table platform.runs add constraint run_gate check(gate in ('IN_PROGRESS','BLOCKED','NEEDS_REVIEW','READY_FOR_ACCEPTANCE'));
create table platform.reconciliations (
    id uuid primary key default gen_random_uuid(),
    workspace_id uuid not null,
    project_id uuid not null,
    attempt_id uuid not null,
    actor_id uuid not null,
    state text not null check(state in ('resolved','unresolved')),
    rationale text not null,
    evidence_ids uuid[] not null,
    created_at timestamptz not null default now(),
    foreign key(workspace_id,project_id,attempt_id) references platform.attempts(workspace_id,project_id,id)
);
alter table platform.reconciliations enable row level security;
revoke all on platform.reconciliations from public;
create trigger immutable_update before update on platform.reconciliations
    for each row execute function platform.immutable_row();
insert into platform.schema_versions(version) values('20261030084610');
