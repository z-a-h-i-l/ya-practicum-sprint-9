import os
import json
import sqlite3
import logging
from typing import Optional, Dict, Any
from datetime import datetime

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("PROFILE_DB_PATH", "/app/data/profiles.db")


def _get_db() -> sqlite3.Connection:
    """Get DB connection with row factory."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Initialize profiles table."""
    conn = _get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL UNIQUE,
            provider TEXT NOT NULL DEFAULT 'yandex',
            email TEXT,
            first_name TEXT,
            last_name TEXT,
            display_name TEXT,
            real_name TEXT,
            birth_date TEXT,
            sex TEXT,
            avatar_url TEXT,
            raw_profile TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()
    logger.info("Profiles DB initialized at %s", DB_PATH)


def fetch_yandex_profile(access_token: str) -> Optional[Dict[str, Any]]:
    """
    Fetch user profile from Yandex using the access token.
    Uses Yandex's userinfo endpoint.
    """
    try:
        resp = requests.get(
            "https://login.yandex.ru/info?format=json",
            headers={
                "Authorization": f"OAuth {access_token}",
                "Accept": "application/json",
            },
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning(
                "Yandex profile request failed: %s %s",
                resp.status_code,
                resp.text,
            )
            return None
        profile = resp.json()
        logger.info("Fetched Yandex profile for user %s", profile.get("id"))
        return profile
    except requests.RequestException as exc:
        logger.error("Yandex profile request error: %s", exc)
        return None


def save_profile(user_id: str, profile: Dict[str, Any], provider: str = "yandex") -> bool:
    """
    Save or update user profile from Yandex in the database.
    Returns True on success.
    """
    try:
        conn = _get_db()
        email = profile.get("default_email") or profile.get("email")
        first_name = profile.get("first_name")
        last_name = profile.get("last_name")
        display_name = profile.get("display_name")
        real_name = profile.get("real_name")
        birth_date = profile.get("birthday")
        sex = profile.get("sex")
        avatar_url = (
            f"https://avatars.yandex.net/get-yapic/{profile.get('default_avatar_id', '')}/islands-200"
            if profile.get("default_avatar_id")
            else None
        )
        raw_profile = json.dumps(profile, ensure_ascii=False)

        existing = conn.execute(
            "SELECT id FROM profiles WHERE user_id = ? AND provider = ?",
            (user_id, provider),
        ).fetchone()

        if existing:
            conn.execute(
                """
                UPDATE profiles SET
                    email = ?,
                    first_name = ?,
                    last_name = ?,
                    display_name = ?,
                    real_name = ?,
                    birth_date = ?,
                    sex = ?,
                    avatar_url = ?,
                    raw_profile = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ? AND provider = ?
                """,
                (
                    email, first_name, last_name, display_name,
                    real_name, birth_date, sex, avatar_url,
                    raw_profile, user_id, provider,
                ),
            )
            logger.info("Updated profile for user %s from %s", user_id, provider)
        else:
            conn.execute(
                """
                INSERT INTO profiles
                    (user_id, provider, email, first_name, last_name,
                     display_name, real_name, birth_date, sex, avatar_url, raw_profile)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id, provider, email, first_name, last_name,
                    display_name, real_name, birth_date, sex, avatar_url, raw_profile,
                ),
            )
            logger.info("Created profile for user %s from %s", user_id, provider)

        conn.commit()
        conn.close()
        return True
    except Exception as exc:
        logger.error("Failed to save profile: %s", exc)
        return False


def get_profile(user_id: str, provider: str = "yandex") -> Optional[Dict[str, Any]]:
    """
    Retrieve saved profile from DB.
    Returns dict or None if not found.
    """
    try:
        conn = _get_db()
        row = conn.execute(
            "SELECT * FROM profiles WHERE user_id = ? AND provider = ? ORDER BY updated_at DESC LIMIT 1",
            (user_id, provider),
        ).fetchone()
        conn.close()
        if row:
            profile = dict(row)
            if profile.get("raw_profile"):
                profile["raw_profile"] = json.loads(profile["raw_profile"])
            return profile
        return None
    except Exception as exc:
        logger.error("Failed to get profile: %s", exc)
        return None