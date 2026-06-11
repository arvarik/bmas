# /opt/bmas/daemon/src/auth.py
"""Shared authentication helpers for daemon routes.

Consolidates bearer-token auth logic used by both files.py and
artifacts.py to avoid code duplication.
"""

from fastapi import Request, HTTPException


def check_bearer_or_pass(request: Request, node_key: str) -> None:
    """Verify BMAS_NODE_KEY bearer auth if the key is configured.

    Dashboard requests have no auth (matches existing pattern — single-user
    homelab). Node requests must present the bearer token.

    Args:
        request: The incoming FastAPI request.
        node_key: The expected BMAS_NODE_KEY value. If empty, auth is disabled.
    """
    if not node_key:
        return  # No key configured — auth disabled
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:].strip()
        if token == node_key:
            return
    # If an Authorization header is present but wrong, reject
    if auth:
        raise HTTPException(status_code=401, detail="Invalid bearer token")
    # No auth header at all — allow (dashboard session)


def require_node_key(request: Request, node_key: str) -> None:
    """Require BMAS_NODE_KEY bearer auth for ingest endpoints.

    Args:
        request: The incoming FastAPI request.
        node_key: The expected BMAS_NODE_KEY value.

    Raises:
        ValueError: If the token is missing or invalid.
    """
    if not node_key:
        return  # No key configured — auth disabled (dev mode)
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise ValueError("Missing bearer token")
    token = auth[7:].strip()
    if token != node_key:
        raise ValueError("Invalid bearer token")
