-- =============================================================================
-- CRM PostgreSQL — OLTP schema + sample data
-- =============================================================================
-- This database represents the transactional CRM system.
-- Debezium captures changes via logical replication (pgoutput) and streams
-- them into Kafka, where ClickHouse consumes them via KafkaEngine.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS customers (
    customer_id      VARCHAR(10) PRIMARY KEY,
    full_name        VARCHAR(200) NOT NULL,
    email            VARCHAR(100) NOT NULL,
    phone            VARCHAR(20),
    registration_date DATE NOT NULL,
    tariff_plan      VARCHAR(20) NOT NULL DEFAULT 'basic',
    status           VARCHAR(20) NOT NULL DEFAULT 'active',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS telemetry (
    telemetry_id     VARCHAR(10) PRIMARY KEY,
    customer_id      VARCHAR(10) NOT NULL REFERENCES customers(customer_id),
    device_type      VARCHAR(30) NOT NULL,
    timestamp        TIMESTAMPTZ NOT NULL,
    metric_name      VARCHAR(50) NOT NULL,
    metric_value     DOUBLE PRECISION NOT NULL,
    unit             VARCHAR(20) NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes for OLTP read paths
CREATE INDEX IF NOT EXISTS idx_telemetry_customer_id
    ON telemetry (customer_id);
CREATE INDEX IF NOT EXISTS idx_telemetry_timestamp
    ON telemetry (timestamp);
CREATE INDEX IF NOT EXISTS idx_telemetry_metric
    ON telemetry (customer_id, metric_name);
CREATE INDEX IF NOT EXISTS idx_customers_email
    ON customers (email);

-- ---------------------------------------------------------------------------
-- Sample data (matches existing ClickHouse sample data)
-- ---------------------------------------------------------------------------

INSERT INTO customers (customer_id, full_name, email, phone, registration_date, tariff_plan, status)
VALUES
    ('C001', 'Иван Петров',     'ivan@example.com',   '+79001000001', '2025-06-15', 'premium', 'active'),
    ('C002', 'Мария Смирнова',  'maria@example.com',  '+79001000002', '2025-07-20', 'basic',   'active'),
    ('C003', 'Алексей Иванов',  'alexey@example.com', '+79001000003', '2025-08-01', 'premium', 'inactive'),
    ('C004', 'John Doe',        'john@example.com',   '+79001000004', '2025-09-10', 'basic',   'active')
ON CONFLICT (customer_id) DO NOTHING;

INSERT INTO telemetry (telemetry_id, customer_id, device_type, timestamp, metric_name, metric_value, unit)
VALUES
    ('T001', 'C001', 'bionic-arm', '2026-01-15 10:30:00+00', 'battery_level',  85.5, 'percent'),
    ('T002', 'C001', 'bionic-arm', '2026-01-15 11:00:00+00', 'signal_strength', 72.1, 'dBm'),
    ('T003', 'C001', 'bionic-leg', '2026-01-15 12:00:00+00', 'battery_level',  60.2, 'percent'),
    ('T004', 'C002', 'bionic-arm', '2026-01-15 09:00:00+00', 'battery_level',  90.0, 'percent'),
    ('T005', 'C002', 'bionic-arm', '2026-01-15 14:00:00+00', 'signal_strength', 68.4, 'dBm'),
    ('T006', 'C003', 'bionic-leg', '2026-01-15 08:00:00+00', 'battery_level',  45.0, 'percent'),
    ('T007', 'C004', 'bionic-arm', '2026-01-15 16:00:00+00', 'battery_level',  78.0, 'percent'),
    ('T008', 'C004', 'bionic-leg', '2026-01-15 17:00:00+00', 'signal_strength', 55.3, 'dBm')
ON CONFLICT (telemetry_id) DO NOTHING;

-- Enable logical replication (Debezium requires wal_level = logical)
-- Note: wal_level is set via command-line in docker-compose, not here.
-- This file only creates the schema and sample data.