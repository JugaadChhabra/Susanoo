-- A project remembers which of its owner's connections its agent may use, so runs can hand them on.
-- [PRD > Connectors and API budget > Credential handling], [PRD > Data model]
-- Only ids are stored: secrets stay in Vault. A revoked connection's id may linger here; reads ignore it.
alter table public.projects
    add column connection_ids uuid[] not null default '{}';

-- Reads are covered by the existing owner-only select on projects. Writes are deliberately NOT granted to
-- authenticated: the API checks every id belongs to the caller before saving, which RLS can't express.
