-- Projects keep the user's rules as plain sentences, plus a creation time so lists sort newest first.
-- [PRD > Non-technical users > Extra API] (PUT /projects/{id}/rules), [PRD > Frontend and API > Screens]
alter table public.projects
    add column rules      text[]      not null default '{}',
    add column created_at timestamptz not null default now();

-- Same access as name and spec: owners may write their own rules (RLS policies already scope rows).
grant insert (rules), update (rules) on public.projects to authenticated;

create index projects_user_id_created_at_idx on public.projects (user_id, created_at desc);
