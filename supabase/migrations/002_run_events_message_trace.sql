-- RunEvent carries a plain-English message and an optional trace link. [PRD > Contracts (locked) > Event shape]
alter table public.run_events
    add column message   text not null default '',
    add column trace_url text;
