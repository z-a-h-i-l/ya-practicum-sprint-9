import os
import logging
from functools import wraps
from urllib.parse import urlencode

from flask import Flask, request, jsonify, redirect
from flask_cors import CORS
from dotenv import load_dotenv

from auth_service import (
    authenticate_user,
    create_session,
    get_session,
    delete_session,
    rotate_session,
    ensure_valid_access_token,
    _decode_token_payload,
    get_authorization_url,
    exchange_code_for_tokens,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, supports_credentials=True, origins=["http://localhost:3000"])

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8001"))

AUTH_SERVICE_URL = os.getenv("AUTH_SERVICE_URL", "http://localhost:8081")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")

SESSION_COOKIE_NAME = "bionicpro_session"
SESSION_MAX_AGE = int(os.getenv("SESSION_MAX_AGE_SECONDS", "600"))  # 10 minutes

# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------


def get_session_from_cookie():
    """Extract the session ID from the cookie."""
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_id:
        return None
    return session_id


def require_session(f):
    """Decorator that ensures a valid session cookie is present."""

    @wraps(f)
    def decorated(*args, **kwargs):
        session_id = get_session_from_cookie()
        if not session_id:
            return jsonify({"error": "No session cookie provided"}), 401
        session = get_session(session_id)
        if not session:
            return jsonify({"error": "Session expired or invalid"}), 401
        return f(session_id, session, *args, **kwargs)

    return decorated


# ---------------------------------------------------------------------------
# Helper: Build session cookie response
# ---------------------------------------------------------------------------


def _set_session_cookie(response, session_id: str, max_age: int = None):
    """Set the HttpOnly, Secure, SameSite session cookie on the response."""
    if max_age is None:
        max_age = SESSION_MAX_AGE
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_id,
        max_age=max_age,
        httponly=True,
        secure=False,  # False for localhost dev
        samesite="Lax",
        path="/",
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({"status": "ok"}), 200


@app.route("/api/auth/login", methods=["GET"])
def login_redirect():
    """
    Redirect the user to Keycloak's authorization endpoint (Authorization Code Flow).
    The frontend should call this endpoint and follow the redirect.
    """
    auth_data = get_authorization_url()
    response = redirect(auth_data["auth_url"])
    # Store the state in a cookie so we can check it on callback
    response.set_cookie(
        "bionicpro_auth_state",
        auth_data["state"],
        max_age=600,
        httponly=True,
        secure=False,
        samesite="Lax",
        path="/api/auth",
    )
    return response


@app.route("/api/auth/callback", methods=["GET"])
def login_callback():
    """
    Handle the callback from Keycloak after successful authentication.
    Exchange the authorization code for tokens and create a session.
    """
    code = request.args.get("code")
    state = request.args.get("state")
    error = request.args.get("error")
    error_description = request.args.get("error_description", "")

    # Check for errors from Keycloak
    if error:
        logger.error("Keycloak returned error: %s - %s", error, error_description)
        return redirect(f"{FRONTEND_URL}?error={error}")

    if not code or not state:
        return redirect(f"{FRONTEND_URL}?error=missing_params")

    # Exchange code for tokens (including PKCE verification)
    tokens = exchange_code_for_tokens(code, state)
    if tokens is None:
        return redirect(f"{FRONTEND_URL}?error=token_exchange_failed")

    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")

    if not access_token or not refresh_token:
        return redirect(f"{FRONTEND_URL}?error=incomplete_tokens")

    # Create server-side session
    session_id = create_session(access_token, refresh_token)

    # Redirect back to frontend with session cookie
    response = redirect(FRONTEND_URL)
    _set_session_cookie(response, session_id)
    # Clear the auth state cookie
    response.set_cookie("bionicpro_auth_state", "", max_age=0, path="/api/auth")
    return response


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    """
    Logout: delete the server-side session and clear the cookie.
    """
    session_id = get_session_from_cookie()
    if session_id:
        delete_session(session_id)

    response = jsonify({"message": "Logged out successfully"})
    response.set_cookie(SESSION_COOKIE_NAME, "", max_age=0, path="/")
    return response, 200


@app.route("/api/auth/session", methods=["GET"])
@app.route("/api/auth/me", methods=["GET"])
@require_session
def session_info(session_id: str, session: dict):
    """
    Return basic user info from the access token payload.
    Available at both /api/auth/session and /api/auth/me for compatibility.
    """
    access_token = session.get("access_token", "")
    payload = _decode_token_payload(access_token)
    if payload:
        return jsonify({
            "username": payload.get("preferred_username", ""),
            "email": payload.get("email", ""),
            "roles": payload.get("realm_access", {}).get("roles", []),
            "session_id": session_id,
            "authenticated": True,
        }), 200
    return jsonify({"error": "Could not decode token"}), 500


@app.route("/api/auth/refresh", methods=["POST"])
@require_session
def refresh(session_id: str, session: dict):
    """
    Explicitly refresh the access token. Rotates the session ID.
    """
    new_token = ensure_valid_access_token(session_id)
    if new_token is None:
        return jsonify({"error": "Session invalid or refresh failed"}), 401

    # Rotate session ID for security
    new_session_id = rotate_session(session_id)
    if new_session_id is None:
        return jsonify({"error": "Session rotation failed"}), 500

    response = jsonify({"message": "Token refreshed"})
    _set_session_cookie(response, new_session_id)
    return response, 200


@app.route("/api/auth/proxy/verify", methods=["GET"])
def verify_proxy():
    """
    Endpoint for upstream services (like the reports API) to validate
    a session and retrieve the current access token.
    Upstream services must call this with the session cookie.
    Returns the current valid access_token so the upstream service
    can forward it to Keycloak-protected resources.
    """
    session_id = get_session_from_cookie()
    if not session_id:
        return jsonify({"error": "No session"}), 401

    # Ensure we have a valid access token (auto-refresh if needed)
    access_token = ensure_valid_access_token(session_id)
    if access_token is None:
        return jsonify({"error": "Session expired"}), 401

    # Rotate session to prevent fixation
    new_session_id = rotate_session(session_id)
    if new_session_id is None:
        return jsonify({"error": "Session rotation failed"}), 500

    response = jsonify({
        "access_token": access_token,
        "token_type": "Bearer",
    })
    _set_session_cookie(response, new_session_id)
    return response, 200


@app.route("/api/auth/proxy/userinfo", methods=["GET"])
def proxy_userinfo():
    """
    Return user info from the access token. Auto-refreshes if needed.
    """
    session_id = get_session_from_cookie()
    if not session_id:
        return jsonify({"error": "No session"}), 401

    access_token = ensure_valid_access_token(session_id)
    if access_token is None:
        return jsonify({"error": "Session expired"}), 401

    payload = _decode_token_payload(access_token)

    if payload:
        # Rotate session
        new_session_id = rotate_session(session_id)
        response = jsonify({
            "username": payload.get("preferred_username", ""),
            "email": payload.get("email", ""),
            "roles": payload.get("realm_access", {}).get("roles", []),
        })
        if new_session_id:
            _set_session_cookie(response, new_session_id)
        return response, 200

    return jsonify({"error": "Could not decode token"}), 500


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------


@app.errorhandler(400)
def bad_request(error):
    return jsonify({"error": "Bad request"}), 400


@app.errorhandler(401)
def unauthorized(error):
    return jsonify({"error": "Unauthorized"}), 401


@app.errorhandler(500)
def internal_error(error):
    return jsonify({"error": "Internal server error"}), 500


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info("Starting BionicPRO Auth Service on %s:%s", HOST, PORT)
    app.run(host=HOST, port=PORT, debug=False)