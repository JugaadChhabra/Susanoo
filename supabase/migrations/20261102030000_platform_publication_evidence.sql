alter table platform.repairs add column validation_report_id uuid;
alter table platform.repairs add column validation_evidence_digest text;
alter table platform.repairs add column publication_record jsonb;
alter table platform.repairs add column validation_patch_digest text;
alter table platform.advanced_reports add column hosted_heldout_exposures integer;
create table platform.heldout_exposure_counts (
    workspace_id uuid not null, project_id uuid not null, split_digest text not null,
    exposures integer not null check(exposures>0), updated_at timestamptz not null default now(),
    primary key(project_id,split_digest),
    foreign key(workspace_id,project_id) references platform.projects(workspace_id,id)
);
alter table platform.heldout_exposure_counts enable row level security;
revoke all on platform.heldout_exposure_counts from public;
alter table platform.repairs add foreign key(workspace_id,project_id,validation_report_id)
    references platform.advanced_reports(workspace_id,project_id,id);
insert into platform.schema_versions(version) values('20261102030000');
