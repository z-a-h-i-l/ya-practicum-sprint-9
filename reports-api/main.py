"""
BionicPRO Reports API — serves pre-built report data mart from ClickHouse OLAP.

Endpoints:
  GET  /health          — health check
  GET  /reports         — return the authenticated user's report
  GET  /reports/periods — return available report periods for the user

Access control:
  - Auth proxy verification via bionicpro-auth service
  - User can only access their own report (customer_id derived from token)
"""

import os
import base64
import json
import logging
from typing import Optional

import clickhouse_connect
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, Query

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="BionicPRO Reports API",
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# ---------------------------------------------------------------------------
# CORS – set in docker-compose via FRONTEND_URL; fallback for local dev
# ---------------------------------------------------------------------------
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
AUTH_SERVICE_URL = os.getenv(
    "AUTH_SERVICE_URL", "http://bionicpro-auth:8081"
)
AUTH_PROXY_VERIFY_URL = f"{AUTH_SERVICE_URL}/api/auth/proxy/verify"

CLICKHOUSE_HOST = os.getenv("CLICKHOUSE_HOST", "clickhouse")
CLICKHOUSE_PORT = int(os.getenv("CLICKHOUSE_PORT", "8123"))
CLICKHOUSE_DB = os.getenv("CLICKHOUSE_DB", "olap_db")
CLICKHOUSE_USER = os.getenv("CLICKHOUSE_USER", "default")
CLICKHOUSE_PASSWORD = os.getenv("CLICKHOUSE_PASSWORD", "clickhouse_password")


def _get_ch_client():
    """Return a ClickHouse client (lazy, created per-request)."""
    return clickhouse_connect.get_client(
        host=CLICKHOUSE_HOST,
        port=CLICKHOUSE_PORT,
        username=CLICKHOUSE_USER,
        password=CLICKHOUSE_PASSWORD,
        database=CLICKHOUSE_DB,
    )


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------


def decode_token_payload_unsafe(token: str) -> Optional[dict]:
    """
    Decode JWT payload without verification (to read claims like sub,
    preferred_username, email).  Used only after the token has been validated
    by bionicpro-auth.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload = parts[1]
        payload += "=" * (4 - len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload)
        return json.loads(decoded)
    except Exception as exc:
        logger.warning("Failed to decode token payload: %s", exc)
        return None


def get_customer_id_from_token(token: str) -> Optional[str]:
    """
    Derive customer_id from the access token.
    Priority:
      1. 'customer_id' claim (if set by Keycloak mapper)
      2. email → customer mapping
      3. 'sub' claim as fallback
    """
    payload = decode_token_payload_unsafe(token)
    if not payload:
        return None

    # Explicit claim
    customer_id = payload.get("customer_id")
    if customer_id:
        return customer_id

    email = payload.get("email", "")
    preferred_username = payload.get("preferred_username", "")
    sub = payload.get("sub", "")

    EMAIL_TO_CUSTOMER = {
        "ivan@example.com": "C001",
        "maria@example.com": "C002",
        "alexey@example.com": "C003",
    }

    if email in EMAIL_TO_CUSTOMER:
        return EMAIL_TO_CUSTOMER[email]

    if preferred_username and preferred_username.startswith("C"):
        return preferred_username

    logger.warning(
        "Could not map token to customer_id. sub=%s, email=%s", sub, email
    )
    return sub


# ---------------------------------------------------------------------------
# Auth proxy
# ---------------------------------------------------------------------------


async def verify_session(request: Request) -> str:
    """
    Verify the session cookie with bionicpro-auth and return a valid access_token.
    Raises HTTPException(401) if the session is invalid.
    """
    cookies = request.headers.get("cookie", "")
    if not cookies:
        raise HTTPException(status_code=401, detail="No session cookie")

    try:
        resp = requests.get(
            AUTH_PROXY_VERIFY_URL,
            headers={"Cookie": cookies},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning("Auth proxy verify failed: %s", resp.status_code)
            raise HTTPException(status_code=401, detail="Invalid session")

        data = resp.json()
        access_token = data.get("access_token")
        if not access_token:
            raise HTTPException(status_code=401, detail="No access token in response")
        return access_token
    except requests.RequestException as exc:
        logger.error("Auth service unreachable: %s", exc)
        raise HTTPException(
            status_code=503, detail="Authentication service unavailable"
        )


# ---------------------------------------------------------------------------
# Helper: convert ClickHouse row to dict
# ---------------------------------------------------------------------------


def _row_to_dict(columns: tuple, row: tuple) -> dict:
    """Convert a ClickHouse result row into a dict, ensuring datetime → ISO str."""
    result = {}
    for col, val in zip(columns, row):
        if hasattr(val, "isoformat"):
            result[col] = val.isoformat()
        else:
            result[col] = val
    return result


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    """Health check endpoint."""
    try:
        client = _get_ch_client()
        client.query("SELECT 1")
        db_status = "ok"
    except Exception as exc:
        db_status = f"error: {exc}"
    return {"status": "ok", "database": db_status}


@app.get("/reports")
async def get_report(
    request: Request,
    period_start: Optional[str] = Query(
        None,
        description="Start of reporting period (ISO date). If not provided, returns the latest available period.",
    ),
    period_end: Optional[str] = Query(
        None,
        description="End of reporting period (ISO date). If not provided, returns the latest available period.",
    ),
):
    """
    Return the user's own report from the ClickHouse OLAP report_mart.

    Access control:
    - Authenticated via bionicpro-auth session cookie
    - User can ONLY access their own report (derived from access token)
    - Returns 404 if no data for the requested period
    """
    # 1. Verify session → get valid access token
    access_token = await verify_session(request)

    # 2. Derive customer_id from token
    customer_id = get_customer_id_from_token(access_token)
    if not customer_id:
        raise HTTPException(
            status_code=403, detail="Cannot determine customer identity from token"
        )

    logger.info("Fetching report for customer_id=%s", customer_id)

    # 3. Query ClickHouse report_mart
    try:
        client = _get_ch_client()

        if period_start and period_end:
            query = """
                SELECT *
                FROM report_mart FINAL
                WHERE customer_id = {customer_id:String}
                  AND report_period_start >= {period_start:DateTime}
                  AND report_period_end <= {period_end:DateTime}
                ORDER BY report_generated_at DESC
                LIMIT 1
            """
            params = {
                "customer_id": customer_id,
                "period_start": period_start,
                "period_end": period_end,
            }
        else:
            query = """
                SELECT *
                FROM report_mart FINAL
                WHERE customer_id = {customer_id:String}
                ORDER BY report_generated_at DESC
                LIMIT 1
            """
            params = {"customer_id": customer_id}

        result = client.query(query, parameters=params)

        if result.row_count == 0:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"No report found for customer {customer_id}. "
                    "The data may not yet be available in ClickHouse OLAP "
                    "(Airflow processes data daily at 02:00 UTC)."
                ),
            )

        row = result.first_row
        columns = result.column_names
        report_data = _row_to_dict(columns, row)

        return {
            "report": report_data,
            "customer_id": customer_id,
            "message": "Report retrieved successfully",
        }

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("ClickHouse query failed: %s", exc)
        raise HTTPException(status_code=500, detail="Database error")


@app.get("/reports/periods")
async def get_available_periods(request: Request):
    """
    Return the list of available report periods for the authenticated user.
    Helps the UI know which periods have been processed by Airflow.
    """
    access_token = await verify_session(request)
    customer_id = get_customer_id_from_token(access_token)
    if not customer_id:
        raise HTTPException(
            status_code=403, detail="Cannot determine customer identity from token"
        )

    try:
        client = _get_ch_client()
        query = """
            SELECT DISTINCT
                report_period_start,
                report_period_end,
                report_generated_at
            FROM report_mart FINAL
            WHERE customer_id = {customer_id:String}
            ORDER BY report_generated_at DESC
        """
        result = client.query(query, parameters={"customer_id": customer_id})

        periods = []
        for row in result.result_rows:
            periods.append(
                {
                    "period_start": (
                        row[0].isoformat() if hasattr(row[0], "isoformat") else str(row[0])
                    ),
                    "period_end": (
                        row[1].isoformat() if hasattr(row[1], "isoformat") else str(row[1])
                    ),
                    "generated_at": (
                        row[2].isoformat() if hasattr(row[2], "isoformat") else str(row[2])
                    ),
                }
            )
        return {"customer_id": customer_id, "available_periods": periods}

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("ClickHouse query failed: %s", exc)
        raise HTTPException(status_code=500, detail="Database error")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    HOST = os.getenv("HOST", "0.0.0.0")
    PORT = int(os.getenv("PORT", "8000"))
    logger.info("Starting BionicPRO Reports API (ClickHouse) on %s:%s", HOST, PORT)
    uvicorn.run("main:app", host=HOST, port=PORT, reload=False)