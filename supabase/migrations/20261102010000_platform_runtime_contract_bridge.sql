alter table platform.attempts add column cleanup_budget integer not null default 0 check(cleanup_budget>=0);
alter table platform.budget_reservations drop constraint budget_reservations_budget_kind_check;
alter table platform.budget_reservations add constraint budget_reservations_budget_kind_check
    check(budget_kind in ('agent','reference','cleanup'));
alter table platform.reconciliations add column content_hash text;
alter table platform.reconciliations add column cleanup_state text
    check(cleanup_state in ('succeeded','failed','not_required'));
create unique index reconciliation_content_key on platform.reconciliations(attempt_id,content_hash);
create table platform.late_runner_events (
    workspace_id uuid not null,
    project_id uuid not null,
    attempt_id uuid not null,
    reconciliation_id uuid not null references platform.reconciliations(id),
    event_id uuid not null,
    sequence integer not null check(sequence>0),
    payload jsonb not null,
    payload_hash text not null,
    received_at timestamptz not null default now(),
    primary key(attempt_id,event_id),
    unique(attempt_id,sequence),
    foreign key(workspace_id,project_id,attempt_id) references platform.attempts(workspace_id,project_id,id)
);
alter table platform.late_runner_events enable row level security;
revoke all on platform.late_runner_events from public;
create trigger immutable_update before update on platform.late_runner_events
    for each row execute function platform.immutable_row();
insert into platform.schema_versions(version) values('20261102010000');
