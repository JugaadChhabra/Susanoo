alter table platform.artifacts add column purpose text not null default 'evidence'
    check(purpose in ('evidence','source','patch','media','export'));
alter table platform.artifacts add column authorization_version_id uuid;
alter table platform.artifacts add foreign key(workspace_id,project_id,authorization_version_id)
    references platform.versions(workspace_id,project_id,id);
insert into platform.schema_versions(version) values('20261030092857');
