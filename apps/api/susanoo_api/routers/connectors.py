"""Connectors and the caller's connections: list with guides, save to Vault, test, revoke.

[PRD › Connectors and API budget], [PRD › Connectors and API budget › Credential handling]
Secrets are written to and read from Supabase Vault through the security-definer functions in
001_init.sql; no response model has a field that could carry one, and nothing here logs a value.
"""

import logging
from typing import Annotated, Any, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from susanoo.connectors import Connector, Credential, InvalidCredential, TestResult, registry
from susanoo.db import session
from susanoo_api.deps import CurrentUserDep
from susanoo_api.models import ConnectionStatus, ConnectionSummary, ConnectionTest, ConnectorInfo, NewConnection

router = APIRouter(tags=["connectors"])

logger = logging.getLogger("susanoo_api.connectors")

_NOT_FOUND = "We couldn't find that connection."
_UNAVAILABLE = "Your connections are unavailable right now. Please try again in a minute."
_CHANGED = "This connection was replaced or revoked while we were testing it. Please test it again."


class ConnectionStore(Protocol):
    async def list_for_user(self, user_id: str) -> list[ConnectionSummary]: ...
    async def save(
        self, user_id: str, connector: str, secret: SecretStr, settings: dict[str, str], writes_enabled: bool
    ) -> ConnectionSummary: ...
    async def credential(self, user_id: str, connection_id: str) -> tuple[ConnectionSummary, Credential, str] | None:
        """(summary, credential, secret_ref); secret_ref changes whenever the secret is replaced."""
        ...

    async def set_status(
        self, user_id: str, connection_id: str, status: ConnectionStatus, secret_ref: str
    ) -> ConnectionSummary | None:
        """Record a test verdict only if the secret is still the one that was tested."""
        ...

    async def delete(self, user_id: str, connection_id: str) -> bool: ...


_COLUMNS = "id::text, connector, status, writes_enabled, settings"


def _summary(row: dict[str, Any]) -> ConnectionSummary:
    return ConnectionSummary(**{k: row[k] for k in ("id", "connector", "status", "writes_enabled", "settings")})


class PostgresConnectionStore:
    async def list_for_user(self, user_id: str) -> list[ConnectionSummary]:
        async with session() as conn:
            cur = await conn.execute(
                f"select {_COLUMNS} from public.connections where user_id = %s order by connector", (user_id,)
            )
            return [_summary(row) for row in await cur.fetchall()]

    async def save(
        self, user_id: str, connector: str, secret: SecretStr, settings: dict[str, str], writes_enabled: bool
    ) -> ConnectionSummary:
        """Store the secret in Vault (replacing and deleting any previous one) and the settings beside it."""
        async with session() as conn:
            cur = await conn.execute(
                "select public.save_connection(%s, %s, %s, %s)::text as id",
                (user_id, connector, secret.get_secret_value(), writes_enabled),
            )
            row = await cur.fetchone()
            assert row is not None
            cur = await conn.execute(
                f"update public.connections set settings = %s where id = %s and user_id = %s returning {_COLUMNS}",
                (Jsonb(settings), row["id"], user_id),
            )
            saved = await cur.fetchone()
            assert saved is not None
            return _summary(saved)

    async def credential(self, user_id: str, connection_id: str) -> tuple[ConnectionSummary, Credential, str] | None:
        async with session() as conn:
            cur = await conn.execute(
                f"select {_COLUMNS}, vault_secret_id, public.read_connection_secret(id) as secret "
                "from public.connections where id = %s and user_id = %s",
                (connection_id, user_id),
            )
            row = await cur.fetchone()
        if row is None or row["secret"] is None:
            return None
        summary = _summary(row)
        credential = Credential(SecretStr(row["secret"]), summary.settings, summary.writes_enabled)
        return summary, credential, row["vault_secret_id"]

    async def set_status(
        self, user_id: str, connection_id: str, status: ConnectionStatus, secret_ref: str
    ) -> ConnectionSummary | None:
        async with session() as conn:
            cur = await conn.execute(
                "update public.connections set status = %s "
                f"where id = %s and user_id = %s and vault_secret_id = %s returning {_COLUMNS}",
                (status, connection_id, user_id, secret_ref),
            )
            row = await cur.fetchone()
            return _summary(row) if row else None

    async def delete(self, user_id: str, connection_id: str) -> bool:
        """The connections_delete_secret trigger removes the Vault secret in the same transaction."""
        async with session() as conn:
            cur = await conn.execute(
                "delete from public.connections where id = %s and user_id = %s", (connection_id, user_id)
            )
            return cur.rowcount > 0


def connection_store() -> ConnectionStore:
    """Tests override this dependency with an in-memory store."""
    return PostgresConnectionStore()


def connectors() -> dict[str, Connector]:
    """Tests override this dependency to point GitHub at a mock transport."""
    return registry()


StoreDep = Annotated[ConnectionStore, Depends(connection_store)]
ConnectorsDep = Annotated[dict[str, Connector], Depends(connectors)]


def _unavailable(action: str, user_id: str) -> HTTPException:
    logger.exception("Could not %s for user %s", action, user_id)
    return HTTPException(503, _UNAVAILABLE)


def _connection_id(raw: str) -> str:
    try:
        return str(UUID(raw))
    except ValueError:
        raise HTTPException(404, _NOT_FOUND) from None


@router.get("/connectors", response_model=list[ConnectorInfo])
async def list_connectors(user: CurrentUserDep, store: StoreDep, available: ConnectorsDep) -> list[ConnectorInfo]:
    try:
        mine = {c.connector: c for c in await store.list_for_user(user.id)}
    except Exception:
        raise _unavailable("list connections", user.id) from None
    return [
        ConnectorInfo(spec=c.spec, guide=c.guide(), connection=mine.get(connector_id))
        for connector_id, c in available.items()
    ]


@router.post("/connections", response_model=ConnectionSummary, status_code=201)
async def save_connection(
    body: NewConnection, user: CurrentUserDep, store: StoreDep, available: ConnectorsDep
) -> ConnectionSummary:
    connector = available.get(body.connector)
    if connector is None:
        raise HTTPException(422, "We don't have that connector.")
    try:
        secret, settings = connector.validate(body.values)
    except InvalidCredential as exc:
        raise HTTPException(422, str(exc)) from None
    try:
        return await store.save(user.id, body.connector, secret, settings, body.writes_enabled)
    except Exception:
        raise _unavailable("save a connection", user.id) from None


@router.post("/connections/{connection_id}/test", response_model=ConnectionTest)
async def test_connection(
    connection_id: str, user: CurrentUserDep, store: StoreDep, available: ConnectorsDep
) -> ConnectionTest:
    canonical = _connection_id(connection_id)
    try:
        found = await store.credential(user.id, canonical)
    except Exception:
        raise _unavailable("read a connection", user.id) from None
    if found is None or found[0].connector not in available:
        raise HTTPException(404, _NOT_FOUND)
    summary, credential, secret_ref = found
    try:
        result = await available[summary.connector].test_connection(credential)
    except Exception:
        logger.exception("Testing a %s connection failed for user %s", summary.connector, user.id)
        result = TestResult(
            ok=False, message=f"We couldn't check this {summary.connector} connection. Please try again."
        )
    try:
        updated = await store.set_status(user.id, canonical, "connected" if result.ok else "failed", secret_ref)
    except Exception:
        raise _unavailable("record a connection test", user.id) from None
    if updated is None:  # replaced or revoked while we were testing it: don't stamp this verdict on the new one
        raise HTTPException(409, _CHANGED)
    return ConnectionTest(ok=result.ok, message=result.message, connection=updated)


@router.delete("/connections/{connection_id}", status_code=204)
async def revoke_connection(connection_id: str, user: CurrentUserDep, store: StoreDep) -> Response:
    canonical = _connection_id(connection_id)
    try:
        deleted = await store.delete(user.id, canonical)
    except Exception:
        raise _unavailable("revoke a connection", user.id) from None
    if not deleted:
        raise HTTPException(404, _NOT_FOUND)
    return Response(status_code=204)
