-- Two saves of the same connector by the same user (a double click, two tabs) must not both read the
-- same old secret: the loser's new Vault secret would be left behind with nothing pointing at it.
-- A transaction-scoped advisory lock per (user, connector) serialises them. [PRD > Credential handling]
create or replace function public.save_connection(
    p_user_id uuid, p_connector text, p_secret text, p_writes_enabled boolean default false
) returns uuid
language plpgsql security definer set search_path = '' as $$
declare
    v_old text;
    v_secret uuid;
    v_id uuid;
begin
    perform pg_advisory_xact_lock(hashtextextended('conn:' || p_user_id || ':' || p_connector, 0));
    select vault_secret_id into v_old from public.connections
        where user_id = p_user_id and connector = p_connector;
    v_secret := vault.create_secret(p_secret, 'conn:' || p_user_id || ':' || p_connector || ':' || gen_random_uuid());
    insert into public.connections (user_id, connector, vault_secret_id, writes_enabled)
        values (p_user_id, p_connector, v_secret::text, p_writes_enabled)
        on conflict (user_id, connector) do update
            set vault_secret_id = excluded.vault_secret_id,
                writes_enabled = excluded.writes_enabled,
                status = 'untested'
        returning id into v_id;
    if v_old is not null then
        delete from vault.secrets where id = v_old::uuid;
    end if;
    return v_id;
end $$;

-- create or replace keeps existing grants; restate them so this file reads on its own.
revoke all on function public.save_connection(uuid, text, text, boolean) from public, anon, authenticated;
grant execute on function public.save_connection(uuid, text, text, boolean) to service_role;
