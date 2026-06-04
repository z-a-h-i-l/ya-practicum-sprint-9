import os
import time
import uuid
import json
import base64
import hashlib
import logging
from typing import Optional, Dict, Any

import requests
import jwt as pyjwt
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

KEYCLOAK_INTERNAL_URL = os.getenv("KEYCLOAK_URL", "http://keycloak:8080")
KEYCLOAK_EXTERNAL_URL = os.getenv("KEYCLOAK_EXTERNAL_URL", "http://localhost:8080")
KEYCLOAK_REALM = os.getenv("KEYCLOAK_REALM", "reports-realm")
KEYCLOAK_CLIENT_ID = os.getenv("KEYCLOAK_CLIENT_ID", "reports-api")
KEYCLOAK_CLIENT_SECRET = os.getenv("KEYCLOAK_CLIENT_SECRET", "")

AUTH_SERVICE_EXTERNAL_URL = os.getenv("AUTH_SERVICE_URL", "http://localhost:8081")
AUTH_SERVICE_INTERNAL_URL = os.getenv("AUTH_SERVICE_INTERNAL_URL", "http://localhost:8081")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")

ACCESS_TOKEN_MAX_AGE = int(os.getenv("ACCESS_TOKEN_MAX_AGE_SECONDS", "120"))
SESSION_MAX_AGE = int(os.getenv("SESSION_MAX_AGE_SECONDS", "600"))

# Internal Keycloak URL — for server-to-server requests (token, introspect, certs)
KEYCLOAK_TOKEN_URL = (
    f"{KEYCLOAK_INTERNAL_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token"
)
KEYCLOAK_CERTS_URL = (
    f"{KEYCLOAK_INTERNAL_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/certs"
)

# External Keycloak authorization URL — for browser redirects
KEYCLOAK_AUTH_URL = (
    f"{KEYCLOAK_EXTERNAL_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/auth"
)

# External callback URL (browser will come back to this)
AUTH_CALLBACK_URL = f"{AUTH_SERVICE_EXTERNAL_URL}/api/auth/callback"


# ---------------------------------------------------------------------------
# In-memory session store (for single-instance / dev)
# In production, replace with Redis / distributed cache.
# ---------------------------------------------------------------------------

# session_id -> {"access_token": str, "refresh_token": str, "created_at": float}
_sessions: Dict[str, Dict[str, Any]] = {}


def _generate_session_id() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------


def _decode_token_payload(token: str) -> Optional[Dict[str, Any]]:
    """
    Decode a JWT without signature verification to extract the payload.
    Useful for reading claims like preferred_username, email, exp, etc.
    """
    try:
        return pyjwt.decode(
            token,
            options={"verify_signature": False},
            algorithms=["RS256", "HS256"],
        )
    except Exception as exc:
        logger.warning("Failed to decode token payload: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Keycloak communication helpers
# ---------------------------------------------------------------------------


def _keycloak_token_request(payload: dict) -> Optional[dict]:
    """Send a token request to Keycloak and return the JSON response."""
    try:
        resp = requests.post(
            KEYCLOAK_TOKEN_URL,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning(
                "Keycloak token request failed: %s %s",
                resp.status_code,
                resp.text,
            )
            return None
        return resp.json()
    except requests.RequestException as exc:
        logger.error("Keycloak token request error: %s", exc)
        return None


def authenticate_user(username: str, password: str) -> Optional[dict]:
    """
    Authenticate user against Keycloak via password grant.
    Returns a dict with 'access_token', 'refresh_token', 'expires_in' etc.
    """
    payload = {
        "client_id": KEYCLOAK_CLIENT_ID,
        "client_secret": KEYCLOAK_CLIENT_SECRET,
        "grant_type": "password",
        "username": username,
        "password": password,
    }
    return _keycloak_token_request(payload)


def refresh_access_token(refresh_token: str) -> Optional[dict]:
    """
    Use a refresh token to obtain a new access token from Keycloak.
    Returns the token response dict, or None on failure.
    """
    payload = {
        "client_id": KEYCLOAK_CLIENT_ID,
        "client_secret": KEYCLOAK_CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    return _keycloak_token_request(payload)


def introspect_token(access_token: str) -> Optional[dict]:
    """
    Introspect token at Keycloak to check validity and get metadata.
    """
    try:
        resp = requests.post(
            f"{KEYCLOAK_INTERNAL_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token/introspect",
            data={
                "client_id": KEYCLOAK_CLIENT_ID,
                "client_secret": KEYCLOAK_CLIENT_SECRET,
                "token": access_token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        return resp.json()
    except requests.RequestException as exc:
        logger.error("Token introspection error: %s", exc)
        return None


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------


# Store code_verifier temporarily for the auth flow (keyed by state)
_pkce_store: Dict[str, str] = {}


def _generate_pkce_pair() -> tuple:
    """Generate a code_verifier and code_challenge for PKCE."""
    code_verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return code_verifier, code_challenge


def get_authorization_url() -> dict:
    """
    Build the Keycloak authorization URL with PKCE support.
    Returns a dict with 'auth_url' and 'state' that the caller should store.
    """
    code_verifier, code_challenge = _generate_pkce_pair()
    state = uuid.uuid4().hex

    # Store code_verifier keyed by state
    _pkce_store[state] = code_verifier

    params = {
        "response_type": "code",
        "client_id": KEYCLOAK_CLIENT_ID,
        "redirect_uri": AUTH_CALLBACK_URL,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "scope": "openid profile email",
    }

    auth_url = KEYCLOAK_AUTH_URL + "?" + "&".join(
        f"{k}={requests.utils.quote(v)}" for k, v in params.items()
    )

    return {
        "auth_url": auth_url,
        "state": state,
    }


def exchange_code_for_tokens(code: str, state: str) -> Optional[dict]:
    """
    Exchange an authorization code (and PKCE code_verifier) for tokens.
    Returns the token response dict, or None on failure.
    """
    code_verifier = _pkce_store.pop(state, None)
    if code_verifier is None:
        logger.error("No code_verifier found for state %s", state)
        return None

    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": AUTH_CALLBACK_URL,
        "client_id": KEYCLOAK_CLIENT_ID,
        "client_secret": KEYCLOAK_CLIENT_SECRET,
        "code_verifier": code_verifier,
    }

    return _keycloak_token_request(payload)


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------


def create_session(access_token: str, refresh_token: str, id_token: str = None) -> str:
    """
    Store tokens in the server-side session store and return a session_id.
    The access_token is kept in memory; the refresh_token is stored encrypted
    (for production, use a distributed cache like Redis).
    """
    session_id = _generate_session_id()
    _sessions[session_id] = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": id_token,
        "created_at": time.time(),
    }
    logger.info("Created session %s", session_id)
    return session_id


def get_keycloak_logout_url(session_id: str, post_logout_redirect_uri: str) -> str | None:
    """
    Build the Keycloak RP-Initiated Logout URL.
    Uses the id_token from the session to hint Keycloak which session to end.
    Returns None if the session does not exist.
    """
    from urllib.parse import urlencode

    session = _sessions.get(session_id)
    if session is None:
        return None
    
    id_token = session.get("id_token")
    params = {
        "post_logout_redirect_uri": post_logout_redirect_uri,
    }
    if id_token:
        params["id_token_hint"] = id_token
    
    return (
        f"{KEYCLOAK_EXTERNAL_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/logout"
        f"?{urlencode(params)}"
    )


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve session data by session_id. Returns None if expired or missing."""
    session = _sessions.get(session_id)
    if session is None:
        return None
    # Check session age
    if time.time() - session["created_at"] > SESSION_MAX_AGE:
        logger.info("Session %s expired, removing", session_id)
        delete_session(session_id)
        return None
    return session


def delete_session(session_id: str) -> None:
    """Remove a session from the store."""
    _sessions.pop(session_id, None)


def rotate_session(session_id: str) -> Optional[str]:
    """
    Rotate the session ID to prevent session fixation attacks.
    Creates a new session_id with the same tokens, deletes the old one,
    and returns the new session_id.
    """
    session = get_session(session_id)
    if session is None:
        return None
    # Create new session with same tokens
    new_session_id = _generate_session_id()
    _sessions[new_session_id] = session.copy()
    _sessions[new_session_id]["created_at"] = time.time()
    # Delete old session
    delete_session(session_id)
    logger.info("Rotated session %s -> %s", session_id, new_session_id)
    return new_session_id


def ensure_valid_access_token(session_id: str) -> Optional[str]:
    """
    Given a session_id, return a valid access_token, automatically
    refreshing it via the stored refresh_token if needed.
    Returns None if the session is invalid or refresh fails.
    """
    session = get_session(session_id)
    if session is None:
        return None

    access_token = session["access_token"]
    refresh_token = session["refresh_token"]

    # Decode the access token (without verification) to check expiry
    try:
        unverified = pyjwt.decode(
            access_token,
            options={"verify_signature": False},
            algorithms=["RS256", "HS256"],
        )
        exp = unverified.get("exp", 0)
        now = time.time()
        # If token is expired or will expire in the next 10 seconds, refresh
        if exp - now < 10:
            logger.info("Access token expired or near expiry, refreshing...")
            new_tokens = refresh_access_token(refresh_token)
            if new_tokens is None:
                logger.error("Failed to refresh access token")
                delete_session(session_id)
                return None
            # Update session with new tokens
            session["access_token"] = new_tokens["access_token"]
            if "refresh_token" in new_tokens:
                session["refresh_token"] = new_tokens["refresh_token"]
            session["created_at"] = time.time()
            _sessions[session_id] = session
            return new_tokens["access_token"]
        return access_token
    except pyjwt.DecodeError:
        logger.warning("Could not decode access token, trying refresh...")
        # Try refreshing anyway
        new_tokens = refresh_access_token(refresh_token)
        if new_tokens is None:
            delete_session(session_id)
            return None
        session["access_token"] = new_tokens["access_token"]
        if "refresh_token" in new_tokens:
            session["refresh_token"] = new_tokens["refresh_token"]
        session["created_at"] = time.time()
        _sessions[session_id] = session
        return new_tokens["access_token"]