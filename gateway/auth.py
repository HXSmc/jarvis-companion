"""Bearer-token auth dependency, checked against the vault token at request time."""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException

from common import config


async def require_bearer(authorization: str | None = Header(default=None)) -> None:
    """Reject the request with 401 unless `Authorization: Bearer <vault token>` matches.

    Calls `common.config.get_token()` per request so a missing/empty vault token
    fails closed (None never matches) and tests can monkeypatch it.
    """
    token = config.get_token()
    if not _bearer_matches(authorization, token):
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


def _bearer_matches(authorization: str | None, token: str | None) -> bool:
    """True when the header is exactly `Bearer <token>`, compared constant-time.

    Compare .encode() bytes: compare_digest(str, str) raises TypeError on
    non-ASCII (a 500), while bytes compare never does (LOW-5). A None/empty
    token never matches (fail closed).
    """
    return bool(
        token
        and authorization
        and hmac.compare_digest(authorization.encode(), f"Bearer {token}".encode())
    )


def require_bearer_any(*names: str):
    """Dependency factory: 401 unless the bearer matches ANY named vault token.

    Role-aware variant of `require_bearer` for endpoints served to more than
    one client kind (`/siri/hey` takes the Shortcut's shared token and the
    iPhone app's `ios_token`). Each name is read per request via
    `config.get_token(name=...)`, so an unset token just never matches.
    """
    if not names:
        raise ValueError("require_bearer_any needs at least one token name")

    async def dependency(authorization: str | None = Header(default=None)) -> None:
        if not any(
            _bearer_matches(authorization, config.get_token(name=name))
            for name in names
        ):
            raise HTTPException(status_code=401, detail="invalid or missing bearer token")

    return dependency
