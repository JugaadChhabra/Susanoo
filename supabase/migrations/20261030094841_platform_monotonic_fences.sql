create sequence platform.resource_fence_seq;
select setval('platform.resource_fence_seq',greatest(1,
    (select coalesce(max(fencing_generation),0)+1 from platform.resource_leases)));
do $$ begin
    if exists(select 1 from pg_roles where rolname='service_role') then
        grant usage,select on platform.resource_fence_seq to service_role;
    end if;
end $$;
insert into platform.schema_versions(version) values('20261030094841');
