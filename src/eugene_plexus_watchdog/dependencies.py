"""FastAPI dependencies for v0.2 auth.

Two main dependencies:

  * `require_initialized` — short-circuits with 503 when the operator
    hasn't yet set a passphrase. Endpoints that are only meaningful
    on a configured install (everything except /healthz and
    /v1/auth/initialize) declare this.

  * `require_operator_session` — validates a `Authorization: Bearer ...`
    token. Raises 401 on missing / malformed / expired / revoked.
    Returns the decoded `TokenPayload` for downstream use.

Service-token verification is a separate dependency
(`require_service_token`) so endpoints can scope themselves correctly
(e.g. POST /v1/identity/links/pending is service-only; PATCH
/v1/identity/constitution is operator-only). v0.2 watchdog only has
operator endpoints, so the service dep is defined here for symmetry
but isn't applied to any watchdog routes yet.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import security
from ._generated.common_models import Problem
from .auth_state import AuthState
from .state import WatchdogState

_bearer_scheme = HTTPBearer(auto_error=False)


def _problem(status_code: int, title: str, detail: str) -> HTTPException:
    """Build a 7807-style HTTPException via the generated Problem model."""
    return HTTPException(
        status_code=status_code,
        detail=Problem(
            type=f"https://github.com/eugene-plexus/watchdog#{title.replace(' ', '-').lower()}",
            title=title,
            status=status_code,
            detail=detail,
            component="watchdog",
        ).model_dump(exclude_none=True),
    )


def require_initialized(request: Request) -> WatchdogState:
    """Returns the watchdog state, ONLY if the operator has set a
    passphrase. Otherwise short-circuits with 503 directing the
    wizard to call POST /v1/auth/initialize first."""
    state: WatchdogState = request.app.state.watchdog_state
    if not state.has_passphrase():
        raise _problem(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Setup required",
            "This Eugene Plexus install has no passphrase set yet. "
            "Complete the first-run wizard's security screen "
            "(POST /v1/auth/initialize) before using other endpoints.",
        )
    return state


def require_operator_session(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> security.TokenPayload:
    """Validate the bearer token and confirm it's an operator session.

    Raises 401 for any auth failure; the response body is a Problem
    JSON so the UI can surface a useful message.
    """
    if creds is None or not creds.credentials:
        raise _problem(
            status.HTTP_401_UNAUTHORIZED,
            "Missing token",
            "Provide a session token via the Authorization: Bearer header.",
        )
    token = creds.credentials
    auth: AuthState = request.app.state.auth_state
    if auth.is_revoked(token):
        raise _problem(
            status.HTTP_401_UNAUTHORIZED,
            "Token revoked",
            "Session was explicitly logged out. Login again to obtain a new token.",
        )
    try:
        payload = security.decode_token(
            token=token,
            signing_key=auth.signing_key,
            expected_audience=security.AUDIENCE_OPERATOR,
        )
    except Exception as e:
        raise _problem(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid token",
            f"Session token rejected: {e}",
        ) from e
    return payload


def require_service_token(
    request: Request,
    expected_kind: str,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> security.TokenPayload:
    """Validate a service-audience bearer token. Used by future
    operator-internal endpoints. Not applied to any v0.2 watchdog
    route yet — defined here so other components can mirror the
    shape when they wire their own auth in."""
    if creds is None or not creds.credentials:
        raise _problem(
            status.HTTP_401_UNAUTHORIZED,
            "Missing token",
            "Provide a service token via the Authorization: Bearer header.",
        )
    token = creds.credentials
    auth: AuthState = request.app.state.auth_state
    expected_audience = f"{security.SERVICE_AUDIENCE_PREFIX}{expected_kind}"
    try:
        payload = security.decode_token(
            token=token,
            signing_key=auth.signing_key,
            expected_audience=expected_audience,
        )
    except Exception as e:
        raise _problem(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid service token",
            f"Service token rejected: {e}",
        ) from e
    return payload
