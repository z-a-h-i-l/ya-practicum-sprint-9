"""
Airflow connection initializer for ClickHouse OLAP.
Creates the required ClickHouse HTTP connection for the ETL DAG.
Run once after Airflow is up: python init_connections.py

Note: The DAG uses clickhouse_connect directly (not Airflow connections),
so this script is optional. It registers the connection for visibility in
the Airflow UI and potential use with Airflow ClickHouse providers.
"""

import os
import time
import logging

import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

AIRFLOW_URL = os.getenv("AIRFLOW_URL", "http://localhost:8082")
AIRFLOW_USERNAME = os.getenv("AIRFLOW_USERNAME", "airflow")
AIRFLOW_PASSWORD = os.getenv("AIRFLOW_PASSWORD", "airflow")
CLICKHOUSE_HOST = os.getenv("CLICKHOUSE_HOST", "clickhouse")
CLICKHOUSE_PORT = os.getenv("CLICKHOUSE_PORT", "8123")
CLICKHOUSE_DB = os.getenv("CLICKHOUSE_DB", "olap_db")
CLICKHOUSE_USER = os.getenv("CLICKHOUSE_USER", "default")
CLICKHOUSE_PASSWORD = os.getenv("CLICKHOUSE_PASSWORD", "clickhouse_password")


def wait_for_airflow(max_attempts: int = 30, delay: int = 5) -> bool:
    """Wait for Airflow API to become available."""
    for i in range(max_attempts):
        try:
            resp = requests.get(f"{AIRFLOW_URL}/api/v1/health", timeout=5)
            if resp.status_code == 200:
                logger.info("Airflow is healthy.")
                return True
        except Exception:
            pass
        logger.info("Waiting for Airflow... (%d/%d)", i + 1, max_attempts)
        time.sleep(delay)
    return False


def create_connections():
    """Create ClickHouse connection via Airflow REST API."""
    # ClickHouse connection (HTTP interface)
    ch_payload = {
        "connection_id": "clickhouse_olap",
        "conn_type": "http",
        "host": CLICKHOUSE_HOST,
        "port": int(CLICKHOUSE_PORT),
        "schema": CLICKHOUSE_DB,
        "login": CLICKHOUSE_USER,
        "password": CLICKHOUSE_PASSWORD,
        "extra": '{"protocol": "http", "database": "' + CLICKHOUSE_DB + '"}',
    }

    resp = requests.post(
        f"{AIRFLOW_URL}/api/v1/connections",
        json=ch_payload,
        auth=(AIRFLOW_USERNAME, AIRFLOW_PASSWORD),
        headers={"Content-Type": "application/json"},
        timeout=10,
    )

    if resp.status_code in (200, 201):
        logger.info("Connection 'clickhouse_olap' created successfully.")
    elif resp.status_code == 409:
        logger.info("Connection 'clickhouse_olap' already exists.")
    else:
        logger.error(
            "Failed to create connection: %s %s", resp.status_code, resp.text
        )


if __name__ == "__main__":
    if wait_for_airflow():
        create_connections()
    else:
        logger.error("Airflow did not become available in time.")
        exit(1)