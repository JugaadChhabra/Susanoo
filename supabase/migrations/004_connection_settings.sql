-- Connections keep their non-secret settings (e.g. the GitHub repo "acme/payments-api") beside the Vault
-- pointer, so the app can say what a connection reaches without ever reading the secret back.
-- [PRD > Connectors and API budget > Connector contract], [PRD > Connectors and API budget > Credential handling]
alter table public.connections
    add column settings jsonb not null default '{}'::jsonb,
    add constraint connections_status_check check (status in ('untested', 'connected', 'failed'));

-- Same access as the other non-secret columns; vault_secret_id stays server-side.
grant select (settings) on public.connections to authenticated;
