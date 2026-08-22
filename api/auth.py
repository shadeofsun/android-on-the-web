"""Bearer-token authentication.

The token is compared with `secrets.compare_digest` so that a wrong guess costs
the same time as a right one. Absence of a token is a startup failure (see
`config.load_settings`), never a silent fallback to "open".
"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from api.config import settings

_bearer = HTTPBearer(auto_error=False, description="API_TOKEN as a Bearer token")

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Missing or invalid credentials. Send 'Authorization: Bearer <API_TOKEN>'.",
    headers={"WWW-Authenticate": "Bearer"},
)


def _token_is_valid(candidate: str) -> bool:
    return secrets.compare_digest(candidate.encode("utf-8"), settings.api_token.encode("utf-8"))


async def require_token(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> str:
    """FastAPI dependency: authorise the caller or raise 401.

    Accepts the standard `Authorization: Bearer <token>` header. For the SSE
    logcat stream (EventSource cannot set headers) a `?token=` query parameter
    is also accepted; that path is opt-in per endpoint via `allow_query_token`.
    """
    if credentials is not None and credentials.scheme.lower() == "bearer":
        if _token_is_valid(credentials.credentials):
            return credentials.credentials
        raise _UNAUTHORIZED

    if getattr(request.state, "allow_query_token", False):
        query_token = request.query_params.get("token", "")
        if query_token and _token_is_valid(query_token):
            return query_token

    raise _UNAUTHORIZED


async def require_token_or_query(request: Request) -> str:
    """Same as `require_token`, but also honours `?token=` (for EventSource)."""
    request.state.allow_query_token = True

    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        candidate = header[7:].strip()
        if _token_is_valid(candidate):
            return candidate

    query_token = request.query_params.get("token", "")
    if query_token and _token_is_valid(query_token):
        return query_token

    raise _UNAUTHORIZED


TokenDep = Annotated[str, Depends(require_token)]
StreamTokenDep = Annotated[str, Depends(require_token_or_query)]
