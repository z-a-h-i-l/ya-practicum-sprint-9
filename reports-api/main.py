"""
BionicPRO Reports API — serves pre-built report data mart from ClickHouse OLAP
with S3/Minio caching and CDN delivery.

Endpoints:
  GET  /health          — health check
  GET  /reports         — return the authenticated user's report (via CDN link)
  GET  /reports/periods — return available report periods for the user
  GET  /reports/direct  — return report data directly (bypasses CDN, for internal use)

Access control:
  - Auth proxy verification via bionicpro-auth service
  - User can only access their own report (customer_id derived from token)

S3 / CDN flow:
  1. Check if report already exists in S3 (Minio).
  2. If exists → return CDN URL immediately.
  3. If not → generate from ClickHouse → store in S3 → return CDN URL.
"""

import os
import io
import json
import base64
import logging
from typing import Optional
from datetime import datetime, timezone
from hashlib import md5

import boto3
import clickhouse_connect
import requests
from botocore.client import Config as BotoConfig
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
    version="2.1.0",
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

# S3 / Minio
S3_ENDPOINT = os.getenv("S3_ENDPOINT", "http://minio:9000")
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY", "minioadmin")
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY", "minioadmin")
S3_BUCKET = os.getenv("S3_BUCKET", "reports")
S3_REGION = os.getenv("S3_REGION", "us-east-1")

# CDN base URL — Nginx reverse proxy that caches S3 objects.
# CDN_BASE_URL is returned to clients (external URL).
# CDN_INTERNAL_URL is used for internal purge requests between Docker services.
CDN_BASE_URL = os.getenv("CDN_BASE_URL", "http://localhost:8083")
CDN_INTERNAL_URL = os.getenv("CDN_INTERNAL_URL", "http://cdn:8083")


def _get_s3_client():
    """Return a boto3 S3 client pointed at Minio."""
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
        region_name=S3_REGION,
        config=BotoConfig(signature_version="s3v4"),
    )


def _get_ch_client():
    """Return a ClickHouse client (lazy, created per-request)."""
    return clickhouse_connect.get_client(
        host=CLICKHOUSE_HOST,
        port=CLICKHOUSE_PORT,
        username=CLICKHOUSE_USER,
        password=CLICKHOUSE_PASSWORD,
        database=CLICKHOUSE_DB,
    )


def _ensure_bucket():
    """Create the S3 bucket if it does not exist."""
    try:
        s3 = _get_s3_client()
        s3.head_bucket(Bucket=S3_BUCKET)
        logger.info("S3 bucket '%s' already exists", S3_BUCKET)
    except Exception:
        logger.info("Creating S3 bucket '%s'", S3_BUCKET)
        s3 = _get_s3_client()
        s3.create_bucket(Bucket=S3_BUCKET)


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
        "john@example.com": "C004",
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
# S3 / CDN helpers
# ---------------------------------------------------------------------------


def _build_s3_key(customer_id: str, period_start: str, period_end: str) -> str:
    """
    Build S3 object key from report parameters.
    Structure: {customer_id}/{period_start}_{period_end}.json
    This enables fast prefix-based listing by customer_id.
    """
    safe_start = period_start.replace(":", "-").replace(" ", "_")
    safe_end = period_end.replace(":", "-").replace(" ", "_")
    return f"{customer_id}/{safe_start}_{safe_end}.json"


def _report_exists_in_s3(customer_id: str, period_start: str, period_end: str) -> bool:
    """Check if a report already exists in S3."""
    try:
        s3 = _get_s3_client()
        key = _build_s3_key(customer_id, period_start, period_end)
        s3.head_object(Bucket=S3_BUCKET, Key=key)
        return True
    except Exception:
        return False


def _get_cdn_url(customer_id: str, period_start: str, period_end: str,
                 version: Optional[str] = None) -> str:
    """
    Build the CDN URL for a report with an optional version query parameter.

    The version parameter (?v=<timestamp>) enables cache busting:
    when a report is regenerated, a new timestamp version creates a
    different URL, causing a cache miss in Nginx.
    """
    key = _build_s3_key(customer_id, period_start, period_end)
    url = f"{CDN_BASE_URL}/{S3_BUCKET}/{key}"
    if version:
        url += f"?v={version}"
    return url


def _upload_report_to_s3(report_data: dict, customer_id: str,
                         period_start: str, period_end: str) -> str:
    """
    Serialize report to JSON and upload to S3.
    Returns the S3 object key.
    """
    s3 = _get_s3_client()
    key = _build_s3_key(customer_id, period_start, period_end)

    json_bytes = json.dumps(report_data, ensure_ascii=False, indent=2).encode("utf-8")

    # Calculate MD5 for Content-MD5 header (base64-encoded)
    content_md5 = md5(json_bytes).digest()
    content_md5_b64 = base64.b64encode(content_md5).decode("ascii")

    s3.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=io.BytesIO(json_bytes),
        ContentType="application/json",
        ContentLength=len(json_bytes),
        ContentMD5=content_md5_b64,
        Metadata={
            "customer_id": customer_id,
            "period_start": period_start,
            "period_end": period_end,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
    )

    logger.info("Uploaded report to S3: s3://%s/%s", S3_BUCKET, key)
    return key


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
# CDN cache invalidation helper
# ---------------------------------------------------------------------------


def _get_s3_object_version(customer_id: str, period_start: str, period_end: str) -> str:
    """
    Retrieve the S3 object's last_modified timestamp as a version string.
    Used for CDN URL versioning — ensures a stable cache key for unchanged objects.
    """
    try:
        s3 = _get_s3_client()
        key = _build_s3_key(customer_id, period_start, period_end)
        resp = s3.head_object(Bucket=S3_BUCKET, Key=key)
        last_modified = resp.get("LastModified")
        if last_modified:
            return str(int(last_modified.timestamp()))
    except Exception:
        pass
    return str(datetime.now(timezone.utc).timestamp())


def _purge_cdn_cache(cdn_path: str) -> bool:
    """
    Send a PURGE request to Nginx with X-Purge header to force refresh.
    The purge location in Nginx bypasses cache and fetches a fresh copy
    from Minio, updating the cached entry atomically.
    Falls back gracefully if the purge endpoint is not available.
    """
    purge_url = f"{CDN_INTERNAL_URL}/purge{cdn_path}"
    try:
        resp = requests.request(
            "PURGE",
            purge_url,
            headers={"X-Purge": "true"},
            timeout=5,
        )
        if resp.status_code in (200, 204, 404):
            logger.info("CDN cache purge triggered for: %s (status=%d)", cdn_path, resp.status_code)
            return True
        else:
            logger.warning("CDN purge returned unexpected status: %d", resp.status_code)
            return False
    except requests.RequestException as exc:
        logger.warning("CDN purge request failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    """Health check endpoint — also reports S3/Minio connectivity."""
    status = {"status": "ok"}

    # Check ClickHouse
    try:
        client = _get_ch_client()
        client.query("SELECT 1")
        status["clickhouse"] = "ok"
    except Exception as exc:
        status["clickhouse"] = f"error: {exc}"

    # Check S3 / Minio
    try:
        s3 = _get_s3_client()
        s3.head_bucket(Bucket=S3_BUCKET)
        status["s3"] = "ok"
    except Exception as exc:
        status["s3"] = f"error: {exc}"

    return status


@app.get("/reports/direct")
async def get_report_direct(
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
    Return the user's report directly from ClickHouse (bypasses S3/CDN).
    Useful for debugging and internal access.
    """
    access_token = await verify_session(request)
    customer_id = get_customer_id_from_token(access_token)
    if not customer_id:
        raise HTTPException(
            status_code=403, detail="Cannot determine customer identity from token"
        )

    logger.info("Fetching direct report for customer_id=%s", customer_id)

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
            "message": "Report retrieved successfully (direct from ClickHouse)",
        }

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("ClickHouse query failed: %s", exc)
        raise HTTPException(status_code=500, detail="Database error")


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
    force_refresh: Optional[bool] = Query(
        False,
        description="Force regeneration: skip S3 check, re-generate, update S3, purge CDN cache.",
    ),
):
    """
    Return the user's report via S3/CDN.

    Flow:
    1. Authenticate user and derive customer_id.
    2. If force_refresh → skip S3 check, generate fresh, upload, purge CDN cache.
    3. Check if report exists in S3.
    4. If exists → return CDN URL.
    5. If not → generate from ClickHouse → upload to S3 → return CDN URL.
    """
    # 1. Verify session → get valid access token
    access_token = await verify_session(request)

    # 2. Derive customer_id from token
    customer_id = get_customer_id_from_token(access_token)
    if not customer_id:
        raise HTTPException(
            status_code=403, detail="Cannot determine customer identity from token"
        )

    logger.info(
        "Fetching report for customer_id=%s (force_refresh=%s)",
        customer_id, force_refresh,
    )

    # 3. Determine effective period
    try:
        client = _get_ch_client()

        if period_start and period_end:
            # Use the requested period directly
            eff_start = period_start
            eff_end = period_end
        else:
            # Look up the latest available period for this customer
            latest_query = """
                SELECT report_period_start, report_period_end
                FROM report_mart FINAL
                WHERE customer_id = {customer_id:String}
                ORDER BY report_generated_at DESC
                LIMIT 1
            """
            latest_result = client.query(
                latest_query,
                parameters={"customer_id": customer_id},
            )

            if latest_result.row_count == 0:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"No report found for customer {customer_id}. "
                        "The data may not yet be available in ClickHouse OLAP "
                        "(Airflow processes data daily at 02:00 UTC)."
                    ),
                )

            eff_start = (
                latest_result.first_row[0].isoformat()
                if hasattr(latest_result.first_row[0], "isoformat")
                else str(latest_result.first_row[0])
            )
            eff_end = (
                latest_result.first_row[1].isoformat()
                if hasattr(latest_result.first_row[1], "isoformat")
                else str(latest_result.first_row[1])
            )

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to determine effective period: %s", exc)
        raise HTTPException(status_code=500, detail="Database error")

    # 4. Ensure S3 bucket exists
    try:
        _ensure_bucket()
    except Exception as exc:
        logger.error("Failed to ensure S3 bucket: %s", exc)

    # 5. If not force_refresh, check S3 cache first
    if not force_refresh:
        try:
            if _report_exists_in_s3(customer_id, eff_start, eff_end):
                # Get S3 object version for cache-busted CDN URL
                version = _get_s3_object_version(customer_id, eff_start, eff_end)
                cdn_url = _get_cdn_url(customer_id, eff_start, eff_end, version=version)
                logger.info(
                    "Report found in S3 for customer_id=%s, returning CDN URL",
                    customer_id,
                )
                return {
                    "report": None,
                    "customer_id": customer_id,
                    "cdn_url": cdn_url,
                    "source": "cdn_cache",
                    "message": "Report available via CDN",
                    "period_start": eff_start,
                    "period_end": eff_end,
                }
        except Exception as exc:
            logger.warning("S3 check failed, falling back to generation: %s", exc)

    # 6. Generate report from ClickHouse
    logger.info(
        "Generating report for customer_id=%s, period=%s → %s",
        customer_id, eff_start, eff_end,
    )

    try:
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
            "period_start": eff_start,
            "period_end": eff_end,
        }

        result = client.query(query, parameters=params)

        if result.row_count == 0:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"No report found for customer {customer_id} "
                    f"in period {eff_start} → {eff_end}."
                ),
            )

        row = result.first_row
        columns = result.column_names
        report_data = _row_to_dict(columns, row)

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("ClickHouse query failed: %s", exc)
        raise HTTPException(status_code=500, detail="Database error")

    # 7. Upload to S3
    try:
        s3_key = _upload_report_to_s3(report_data, customer_id, eff_start, eff_end)
        logger.info("Report uploaded to S3: %s", s3_key)
    except Exception as exc:
        logger.error("Failed to upload report to S3: %s", exc)
        # Fallback: return data directly if S3 upload fails
        return {
            "report": report_data,
            "customer_id": customer_id,
            "cdn_url": None,
            "source": "direct_fallback",
            "message": "Report generated but S3 upload failed; returning data directly",
            "period_start": eff_start,
            "period_end": eff_end,
        }

    # 8. Purge CDN cache for this object if force_refresh
    if force_refresh:
        cdn_path = f"/{S3_BUCKET}/{s3_key}"
        _purge_cdn_cache(cdn_path)

    # 9. Return CDN URL with version for cache busting
    version = _get_s3_object_version(customer_id, eff_start, eff_end)
    cdn_url = _get_cdn_url(customer_id, eff_start, eff_end, version=version)
    return {
        "report": None,
        "customer_id": customer_id,
        "cdn_url": cdn_url,
        "source": "generated",
        "message": "Report generated and stored in S3; available via CDN",
        "period_start": eff_start,
        "period_end": eff_end,
    }


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
    logger.info("Starting BionicPRO Reports API (ClickHouse + S3/CDN) on %s:%s", HOST, PORT)
    uvicorn.run("main:app", host=HOST, port=PORT, reload=False)