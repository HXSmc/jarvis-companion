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
    # Compare .encode() bytes: compare_digest(str, str) raises TypeError on
    # non-ASCII (a 500), while bytes compare never does (LOW-5).
    if not token or not authorization or not hmac.compare_digest(
        authorization.encode(), f"Bearer {token}".encode()
    ):
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")
