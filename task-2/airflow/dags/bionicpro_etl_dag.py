"""
BionicPRO ETL DAG — extracts CRM data, loads into OLAP (ClickHouse), builds report data mart.

Расписание: ежедневно в 02:00 UTC.
Извлечение данных о клиентах из CRM-системы, загрузка в ClickHouse
и подготовка витрины (report_mart) для сервиса отчётов.
"""

import logging
from datetime import datetime, timedelta

import pandas as pd
import pendulum
import requests
import clickhouse_connect
from airflow import DAG
from airflow.operators.python import PythonOperator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# ClickHouse connection
CLICKHOUSE_HOST = "clickhouse"
CLICKHOUSE_PORT = 8123
CLICKHOUSE_DB = "olap_db"
CLICKHOUSE_USER = "default"
CLICKHOUSE_PASSWORD = "clickhouse_password"

CRM_API_BASE_URL = "http://crm-mock:8090/api/v1"
TELEMETRY_API_BASE_URL = "http://telemetry-mock:8091/api/v1"

DEFAULT_ARGS = {
    "owner": "bionicpro",
    "depends_on_past": False,
    "start_date": pendulum.datetime(2026, 1, 1, tz="UTC"),
    "email_on_failure": False,
    "email_on_retry": False,
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
}


def _get_ch_client():
    """Create a ClickHouse client."""
    return clickhouse_connect.get_client(
        host=CLICKHOUSE_HOST,
        port=CLICKHOUSE_PORT,
        username=CLICKHOUSE_USER,
        password=CLICKHOUSE_PASSWORD,
        database=CLICKHOUSE_DB,
    )


# ---------------------------------------------------------------------------
# Helper: Fetch CRM customers
# ---------------------------------------------------------------------------


def _extract_crm_customers() -> pd.DataFrame:
    """
    Extract customer data from the CRM system.
    Falls back to generating sample data if CRM is unavailable (dev mode).
    """
    try:
        resp = requests.get(f"{CRM_API_BASE_URL}/customers", timeout=15)
        resp.raise_for_status()
        customers = resp.json()
        logger.info("Fetched %d customers from CRM.", len(customers))
        return pd.DataFrame(customers)
    except Exception as exc:
        logger.warning("CRM API unavailable (%s), using sample data.", exc)
        return _generate_sample_customers()


def _generate_sample_customers() -> pd.DataFrame:
    """Generate sample CRM customer data for development/testing."""
    data = [
        {
            "customer_id": "C001",
            "full_name": "Иван Петров",
            "email": "ivan@example.com",
            "phone": "+79001000001",
            "registration_date": "2025-06-15",
            "tariff_plan": "premium",
            "status": "active",
        },
        {
            "customer_id": "C002",
            "full_name": "Мария Смирнова",
            "email": "maria@example.com",
            "phone": "+79001000002",
            "registration_date": "2025-07-20",
            "tariff_plan": "basic",
            "status": "active",
        },
        {
            "customer_id": "C003",
            "full_name": "Алексей Иванов",
            "email": "alexey@example.com",
            "phone": "+79001000003",
            "registration_date": "2025-08-01",
            "tariff_plan": "premium",
            "status": "inactive",
        },
    ]
    return pd.DataFrame(data)


# ---------------------------------------------------------------------------
# Helper: Extract telemetry
# ---------------------------------------------------------------------------


def _extract_telemetry() -> pd.DataFrame:
    """
    Extract telemetry data from the telemetry service.
    Falls back to generating sample data if unavailable (dev mode).
    """
    try:
        resp = requests.get(f"{TELEMETRY_API_BASE_URL}/telemetry", timeout=15)
        resp.raise_for_status()
        records = resp.json()
        logger.info("Fetched %d telemetry records.", len(records))
        return pd.DataFrame(records)
    except Exception as exc:
        logger.warning("Telemetry API unavailable (%s), using sample data.", exc)
        return _generate_sample_telemetry()


def _generate_sample_telemetry() -> pd.DataFrame:
    """Generate sample telemetry data for development/testing."""
    data = [
        {"telemetry_id": "T001", "customer_id": "C001", "device_type": "bionic-arm",
         "timestamp": "2026-01-15T10:30:00Z", "metric_name": "battery_level",
         "metric_value": 85.5, "unit": "percent"},
        {"telemetry_id": "T002", "customer_id": "C001", "device_type": "bionic-arm",
         "timestamp": "2026-01-15T11:00:00Z", "metric_name": "signal_strength",
         "metric_value": 72.1, "unit": "dBm"},
        {"telemetry_id": "T003", "customer_id": "C001", "device_type": "bionic-leg",
         "timestamp": "2026-01-15T12:00:00Z", "metric_name": "battery_level",
         "metric_value": 60.2, "unit": "percent"},
        {"telemetry_id": "T004", "customer_id": "C002", "device_type": "bionic-arm",
         "timestamp": "2026-01-15T09:00:00Z", "metric_name": "battery_level",
         "metric_value": 90.0, "unit": "percent"},
        {"telemetry_id": "T005", "customer_id": "C002", "device_type": "bionic-arm",
         "timestamp": "2026-01-15T14:00:00Z", "metric_name": "signal_strength",
         "metric_value": 68.4, "unit": "dBm"},
        {"telemetry_id": "T006", "customer_id": "C003", "device_type": "bionic-leg",
         "timestamp": "2026-01-15T08:00:00Z", "metric_name": "battery_level",
         "metric_value": 45.0, "unit": "percent"},
    ]
    return pd.DataFrame(data)


# ---------------------------------------------------------------------------
# ClickHouse table initialisation
# ---------------------------------------------------------------------------


def _ensure_tables() -> None:
    """Create ClickHouse tables if they don't exist."""
    client = _get_ch_client()

    client.command("""
        CREATE TABLE IF NOT EXISTS customers (
            customer_id String,
            full_name String,
            email String,
            phone String,
            registration_date String,
            tariff_plan String,
            status String,
            processed_at DateTime
        ) ENGINE = MergeTree()
        ORDER BY customer_id
    """)

    client.command("""
        CREATE TABLE IF NOT EXISTS telemetry (
            telemetry_id String,
            customer_id String,
            device_type String,
            timestamp DateTime,
            metric_name String,
            metric_value Float64,
            unit String,
            processed_at DateTime
        ) ENGINE = MergeTree()
        ORDER BY (customer_id, timestamp)
    """)

    client.command("""
        CREATE TABLE IF NOT EXISTS report_mart (
            customer_id String,
            full_name String,
            email String,
            phone String,
            registration_date String,
            tariff_plan String,
            status String,
            total_measurements UInt64,
            device_types String,
            metrics_collected String,
            avg_battery_level Nullable(Float64),
            max_signal_strength Nullable(Float64),
            first_measurement_ts Nullable(DateTime),
            last_measurement_ts Nullable(DateTime),
            report_generated_at DateTime,
            report_period_start DateTime,
            report_period_end DateTime
        ) ENGINE = ReplacingMergeTree()
        ORDER BY (customer_id, report_period_start)
    """)

    logger.info("ClickHouse tables ensured.")


# ---------------------------------------------------------------------------
# Load helpers
# ---------------------------------------------------------------------------


def _load_df_to_clickhouse(df: pd.DataFrame, table_name: str) -> None:
    """Write a DataFrame into ClickHouse (truncate + insert)."""
    if df.empty:
        logger.info("DataFrame is empty, skipping load to %s.", table_name)
        return

    client = _get_ch_client()

    # Truncate before loading (simple full-refresh for dev)
    client.command(f"TRUNCATE TABLE IF EXISTS {table_name}")

    # Convert DataFrame to list of tuples for insert
    columns = list(df.columns)
    rows = [tuple(row) for row in df.to_numpy()]
    col_names = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))

    client.command(
        f"INSERT INTO {table_name} ({col_names}) VALUES",
        iter(rows),
    )
    logger.info("Loaded %d rows into %s via ClickHouse insert.", len(df), table_name)


def _normalize_and_load_customers(**kwargs) -> None:
    """Extract CRM customers and load into ClickHouse olap_db.customers."""
    _ensure_tables()
    df = _extract_crm_customers()
    df["processed_at"] = pd.Timestamp.utcnow()
    _load_df_to_clickhouse(df, "customers")


def _normalize_and_load_telemetry(**kwargs) -> None:
    """Extract telemetry records and load into ClickHouse olap_db.telemetry."""
    _ensure_tables()
    df = _extract_telemetry()
    df["processed_at"] = pd.Timestamp.utcnow()
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    _load_df_to_clickhouse(df, "telemetry")


# ---------------------------------------------------------------------------
# Build report data mart (ClickHouse-native aggregation)
# ---------------------------------------------------------------------------


def _build_report_mart(**kwargs) -> None:
    """
    Build the report_mart — a denormalized table joining customers with
    aggregated telemetry data, grouped by customer.

    Uses ClickHouse's native INSERT ... SELECT for high-performance aggregation.
    """
    _ensure_tables()
    client = _get_ch_client()

    # Check if source tables have data
    cust_count = client.query("SELECT count() FROM customers").first_row[0]
    if cust_count == 0:
        logger.warning("No customers found, skipping mart build.")
        return

    # Build mart via ClickHouse INSERT ... SELECT (fast, in-database aggregation)
    client.command("""
        INSERT INTO report_mart
        SELECT
            c.customer_id,
            c.full_name,
            c.email,
            c.phone,
            c.registration_date,
            c.tariff_plan,
            c.status,
            count(t.telemetry_id) AS total_measurements,
            if(
                count(t.telemetry_id) > 0,
                arrayStringConcat(arraySort(groupUniqArray(t.device_type)), ', '),
                ''
            ) AS device_types,
            if(
                count(t.telemetry_id) > 0,
                arrayStringConcat(arraySort(groupUniqArray(t.metric_name)), ', '),
                ''
            ) AS metrics_collected,
            avgIf(t.metric_value, t.metric_name = 'battery_level') AS avg_battery_level,
            maxIf(t.metric_value, t.metric_name = 'signal_strength') AS max_signal_strength,
            min(t.timestamp) AS first_measurement_ts,
            max(t.timestamp) AS last_measurement_ts,
            now() AS report_generated_at,
            toDateTime('2026-01-01 00:00:00', 'UTC') AS report_period_start,
            now() AS report_period_end
        FROM customers AS c
        LEFT JOIN telemetry AS t ON c.customer_id = t.customer_id
        GROUP BY
            c.customer_id,
            c.full_name,
            c.email,
            c.phone,
            c.registration_date,
            c.tariff_plan,
            c.status
    """)

    result = client.query("SELECT count() FROM report_mart")
    count = result.first_row[0]
    logger.info("Report mart built with %d rows (ClickHouse aggregation).", count)


# ---------------------------------------------------------------------------
# DAG definition
# ---------------------------------------------------------------------------

with DAG(
    dag_id="bionicpro_etl",
    default_args=DEFAULT_ARGS,
    description="ETL pipeline: CRM + telemetry → ClickHouse OLAP → report mart",
    schedule=None,  # Batch ETL deactivated — replaced by CDC (Debezium + Kafka → ClickHouse)
    catchup=False,
    max_active_runs=1,
    tags=["bionicpro", "etl", "reports", "clickhouse"],
    doc_md=__doc__,
) as dag:

    extract_load_customers = PythonOperator(
        task_id="extract_load_customers",
        python_callable=_normalize_and_load_customers,
    )

    extract_load_telemetry = PythonOperator(
        task_id="extract_load_telemetry",
        python_callable=_normalize_and_load_telemetry,
    )

    build_mart = PythonOperator(
        task_id="build_report_mart",
        python_callable=_build_report_mart,
    )

    [extract_load_customers, extract_load_telemetry] >> build_mart