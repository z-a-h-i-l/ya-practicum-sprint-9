import os
import logging
from functools import wraps

from flask import Flask, request, jsonify
from dotenv import load_dotenv

from auth_service import (
    authenticate_user,
    create_session,
    get_session,
    delete_session,
    rotate_session,
    ensure_valid_access_token,
    _decode_token_payload,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8001"))

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
        secure=True,
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


@app.route("/api/auth/login", methods=["POST"])
def login():
    """
    Authenticate user with username/password.
    Accepts both JSON and form-urlencoded bodies.
    Returns a session cookie upon successful authentication.
    No tokens are sent to the frontend.
    """
    data = request.get_json(silent=True)
    if not data:
        data = request.form

    username = data.get("username")
    password = data.get("password")
    if not username or not password:
        return jsonify({"error": "Username and password are required"}), 400

    # Authenticate against Keycloak
    tokens = authenticate_user(username, password)
    if tokens is None:
        return jsonify({"error": "Invalid credentials"}), 401

    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")

    if not access_token or not refresh_token:
        return jsonify({"error": "Token response incomplete"}), 502

    # Create server-side session
    session_id = create_session(access_token, refresh_token)

    # Return success with session cookie (no tokens sent to frontend)
    response = jsonify({
        "message": "Login successful",
        "username": username,
    })
    _set_session_cookie(response, session_id)
    return response, 200


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