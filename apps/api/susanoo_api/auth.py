"""Verify the Supabase access token sent as `Authorization: Bearer <jwt>`. [PRD › Frontend and API › Auth flow]

Supabase signs tokens with either the project's legacy shared secret (HS256, SUPABASE_JWT_SECRET)
or an asymmetric key published at `<SUPABASE_URL>/auth/v1/.well-known/jwks.json` (ES256/RS256).
We accept both and nothing else, so `alg: none` and algorithm-confusion tokens are rejected.
"""

import logging
import os
from functools import lru_cache

import jwt
from fastapi import HTTPException, Request
from jwt import PyJWKClient
from pydantic import BaseModel

logger = logging.getLogger("susanoo_api.auth")

AUDIENCE = "authenticated"
_ASYMMETRIC = ["ES256", "RS256"]
_UNAUTHORIZED = "Please log in to continue."


class CurrentUser(BaseModel):
    id: str
    email: str | None = None


def _unauthorized(message: str = _UNAUTHORIZED) -> HTTPException:
    return HTTPException(401, message, headers={"WWW-Authenticate": "Bearer"})


@lru_cache(maxsize=4)
def _jwks_client(url: str) -> PyJWKClient:
    return PyJWKClient(f"{url}/auth/v1/.well-known/jwks.json", cache_keys=True)


def verify_token(token: str) -> CurrentUser:
    """Return the user a valid Supabase access token belongs to, or raise a 401."""
    try:
        alg = jwt.get_unverified_header(token).get("alg")
        if alg == "HS256":
            secret = os.environ.get("SUPABASE_JWT_SECRET", "")
            if not secret:
                logger.error("SUPABASE_JWT_SECRET is not set; rejecting every HS256 token.")
                raise _unauthorized()
            key: object = secret
            algorithms = ["HS256"]
        elif alg in _ASYMMETRIC:
            url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
            if not url:
                logger.error("SUPABASE_URL is not set; cannot verify asymmetric tokens.")
                raise _unauthorized()
            key = _jwks_client(url).get_signing_key_from_jwt(token).key
            algorithms = _ASYMMETRIC
        else:
            raise _unauthorized()
        claims = jwt.decode(
            token,
            key,  # type: ignore[arg-type]
            algorithms=algorithms,
            audience=AUDIENCE,
            options={"require": ["exp", "sub"]},
        )
    except HTTPException:
        raise
    except jwt.ExpiredSignatureError:
        raise _unauthorized("Your session has expired. Please log in again.") from None
    except jwt.PyJWTError:
        raise _unauthorized() from None
    if claims.get("role") != "authenticated":
        raise _unauthorized()
    return CurrentUser(id=str(claims["sub"]), email=claims.get("email"))


async def current_user(request: Request) -> CurrentUser:
    """FastAPI dependency: the logged-in user, or a 401 in the standard error shape."""
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _unauthorized()
    return verify_token(token.strip())
