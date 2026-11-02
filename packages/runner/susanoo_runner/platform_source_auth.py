"""Attempt-scoped source credentials. No credential enters a build environment or Git configuration file."""

import base64
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from urllib.parse import urlsplit

from susanoo_contracts import SourceCredentialResponse

from .journal import JournalError


@asynccontextmanager
async def source_auth(claim, journal, source, *, enabled):
    if not enabled:
        yield None
        return
    version = next(value for value in claim.execution_manifest.versions if value.kind == "source")
    spec = version.data
    if spec.source_kind != "git" or not hasattr(source, "url"):
        raise JournalError("source_credential_refused", "Only Git sources may use the source credential broker")
    url = urlsplit(source.url)
    repository = url.path.strip("/").removesuffix(".git")
    if (
        spec.source_kind != "git"
        or not spec.trusted
        or not spec.installation_id
        or url.scheme != "https"
        or url.hostname != "github.com"
        or url.port not in {None, 443}
        or url.username
        or url.password
        or url.query
        or url.fragment
        or repository != spec.repository
    ):
        raise JournalError(
            "source_credential_refused", "Broker credentials require the exact approved GitHub repository"
        )
    body = {"lease_token": claim.lease_token, "source_version_id": str(version.id)}
    endpoint = journal.endpoint + "/source-credential"
    requested = False
    try:
        journal.control()
        requested = True
        response = SourceCredentialResponse.model_validate(journal.client.post(endpoint, body))
        if (
            response.repository != spec.repository
            or response.source_commit != spec.commit
            or not response.token
            or len(response.token) > 4096
            or response.expires_at.tzinfo is None
            or response.expires_at <= datetime.now(UTC)
        ):
            raise JournalError("source_credential_refused", "Source credential does not match the pinned assignment")
        encoded = base64.b64encode(("x-access-token:" + response.token).encode()).decode("ascii")
        yield "Authorization: Basic " + encoded
    finally:
        # The read-only control endpoint remains usable for revocation after lease expiry.
        # If its acknowledgement is lost, the platform sweeper retains the encrypted grant.
        if requested:
            journal.client.post(endpoint + "/revoke", body)
