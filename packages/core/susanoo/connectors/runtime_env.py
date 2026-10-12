"""A run's picked connections as the env vars its generated app's container starts with.

[PRD › Connectors and API budget › Credential handling]; IMPLEMENTATION_JUGAAD P6 (#26, slice #30).

`connection_env(user_id, connection_ids)` is the one place secrets leave Vault for a container: each
connection's secret is read through the service-role `read_connection_secret()`, turned into env vars by its
connector's own `env()` (which maps the connection's write setting to the connector's write flag, e.g.
`GITHUB_WRITES_ENABLED`), and registered for redaction before it is returned, so it can't reach a log line or
a trace. Revoked connections (deleted rows) are skipped; another user's connection is refused outright and its
secret is never read. Nothing here logs or raises with a value in it.
"""

import logging
from collections.abc import Mapping, Sequence

from pydantic import SecretStr

from susanoo.connectors import registry
from susanoo.connectors.base import _SECRET_KEYS, Connector, Credential, InvalidCredential, redact_secret_values
from susanoo.db import session

logger = logging.getLogger("susanoo.connectors.runtime_env")


class ConnectionNotYours(PermissionError):
    """A connection id belongs to another user. The message is plain English and names no one."""


async def connection_env(
    user_id: str, connection_ids: Sequence[str], connectors: Mapping[str, Connector] | None = None
) -> dict[str, str]:
    """{env var name: value} for the user's connections, read from Vault now. Raises ConnectionNotYours."""
    if not connection_ids:
        return {}
    connectors = registry() if connectors is None else connectors
    async with session() as conn:
        cur = await conn.execute(
            # the secret is only read for the caller's own rows
            "select id::text, connector, settings, writes_enabled, user_id::text = %(user)s as mine, "
            "case when user_id::text = %(user)s then public.read_connection_secret(id) end as secret "
            "from public.connections where id::text = any(%(ids)s) order by connector",
            {"user": user_id, "ids": [str(i) for i in connection_ids]},
        )
        rows = await cur.fetchall()
    if any(not row["mine"] for row in rows):
        raise ConnectionNotYours("A connection picked for this run belongs to someone else, so none were used.")

    env: dict[str, str] = {}
    found = {row["id"] for row in rows}
    for missing in sorted({str(i) for i in connection_ids} - found):
        logger.warning("Connection %s was revoked, so the agent's app starts without it", missing)
    for row in rows:
        connector = connectors.get(row["connector"])
        if connector is None or row["secret"] is None:
            logger.warning("Connection %s (%s) can't be used right now, so it was skipped", row["id"], row["connector"])
            continue
        credential = Credential(SecretStr(row["secret"]), dict(row["settings"] or {}), bool(row["writes_enabled"]))
        redact_secret_values([row["secret"]])  # before it can reach the container, a log line or a trace
        try:
            values = connector.env(credential)
        except InvalidCredential as exc:  # plain English, never contains the secret
            logger.warning("Connection %s (%s) was skipped: %s", row["id"], row["connector"], exc)
            continue
        redact_secret_values(v for k, v in values.items() if _SECRET_KEYS.search(k))  # e.g. a reformatted token
        env.update(values)
    return env


async def run_connection_env(run_id: str) -> dict[str, str]:
    """`connection_env()` for the owner and picked connections of the project this run belongs to."""
    async with session() as conn:
        cur = await conn.execute(
            "select p.user_id::text as user_id, p.connection_ids::text[] as connection_ids "
            "from public.runs r join public.projects p on p.id = r.project_id where r.id::text = %s",
            (run_id,),
        )
        row = await cur.fetchone()
    if row is None:
        logger.warning("Run %s has no project, so its app starts without connections", run_id)
        return {}
    return await connection_env(row["user_id"], list(row["connection_ids"] or []))
