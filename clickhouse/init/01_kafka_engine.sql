-- =============================================================================
-- ClickHouse KafkaEngine — CDC-поток из Kafka
-- =============================================================================
-- Каждая таблица *_kafka потребляет отдельный топик Kafka как очередь.
-- Таблицы *_mv (MaterializedView) автоматически парсят JSON из KafkaEngine
-- и вставляют очищенные строки в целевые таблицы-дублёры (*_cdc).
--
-- CDC flow:
--   CRM PostgreSQL → Debezium → Kafka → ClickHouse KafkaEngine → MV → *_cdc
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. KafkaEngine: customers_cdc_kafka
--    Потребляет топик crm.public.customers
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS customers_cdc_kafka
(
    -- KafkaEngine оставляет сырой JSON в строке (JSONAsString)
    raw_json String
)
ENGINE = Kafka
SETTINGS
    kafka_broker_list = 'kafka:9092',
    kafka_topic_list = 'crm.public.customers',
    kafka_group_name = 'clickhouse_customers_cdc',
    kafka_format = 'JSONAsString',
    kafka_num_consumers = 1,
    kafka_max_block_size = 1048576,
    kafka_skip_broken_messages = 100;

-- ---------------------------------------------------------------------------
-- 2. Целевая таблица: customers_cdc (ReplacingMergeTree)
--    Содержит актуальное состояние таблицы CRM.public.customers
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS customers_cdc
(
    customer_id      String,
    full_name        String,
    email            String,
    phone            String,
    registration_date String,
    tariff_plan      String,
    status           String,
    clickhouse_arrived_at DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree()
ORDER BY customer_id
SETTINGS index_granularity = 8192;

-- ---------------------------------------------------------------------------
-- 3. MaterializedView: customers_cdc_mv
--    Автоматически парсит JSON из KafkaEngine-таблицы и вставляет в customers_cdc
-- ---------------------------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS customers_cdc_mv
TO customers_cdc
AS
SELECT
    JSONExtractString(raw_json, 'customer_id')       AS customer_id,
    JSONExtractString(raw_json, 'full_name')         AS full_name,
    JSONExtractString(raw_json, 'email')             AS email,
    JSONExtractString(raw_json, 'phone')             AS phone,
    JSONExtractString(raw_json, 'registration_date') AS registration_date,
    JSONExtractString(raw_json, 'tariff_plan')       AS tariff_plan,
    JSONExtractString(raw_json, 'status')            AS status,
    now()                                            AS clickhouse_arrived_at
FROM customers_cdc_kafka
WHERE JSONExtractString(raw_json, 'customer_id') != '';

-- =============================================================================
-- 4. KafkaEngine: telemetry_cdc_kafka
--    Потребляет топик crm.public.telemetry
-- =============================================================================
CREATE TABLE IF NOT EXISTS telemetry_cdc_kafka
(
    raw_json String
)
ENGINE = Kafka
SETTINGS
    kafka_broker_list = 'kafka:9092',
    kafka_topic_list = 'crm.public.telemetry',
    kafka_group_name = 'clickhouse_telemetry_cdc',
    kafka_format = 'JSONAsString',
    kafka_num_consumers = 1,
    kafka_max_block_size = 1048576,
    kafka_skip_broken_messages = 100;

-- ---------------------------------------------------------------------------
-- 5. Целевая таблица: telemetry_cdc (ReplacingMergeTree)
--    Содержит актуальное состояние таблицы CRM.public.telemetry
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS telemetry_cdc
(
    telemetry_id        String,
    customer_id         String,
    device_type         String,
    timestamp           DateTime,
    metric_name         String,
    metric_value        Float64,
    unit                String,
    clickhouse_arrived_at DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree()
ORDER BY telemetry_id
SETTINGS index_granularity = 8192;

-- ---------------------------------------------------------------------------
-- 6. MaterializedView: telemetry_cdc_mv
--    Автоматически парсит JSON из KafkaEngine-таблицы и вставляет в telemetry_cdc
-- ---------------------------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry_cdc_mv
TO telemetry_cdc
AS
SELECT
    JSONExtractString(raw_json, 'telemetry_id')    AS telemetry_id,
    JSONExtractString(raw_json, 'customer_id')     AS customer_id,
    JSONExtractString(raw_json, 'device_type')     AS device_type,
    parseDateTimeBestEffortOrNull(JSONExtractString(raw_json, 'timestamp')) AS timestamp,
    JSONExtractString(raw_json, 'metric_name')     AS metric_name,
    JSONExtractFloat(raw_json, 'metric_value')     AS metric_value,
    JSONExtractString(raw_json, 'unit')            AS unit,
    now()                                           AS clickhouse_arrived_at
FROM telemetry_cdc_kafka
WHERE JSONExtractString(raw_json, 'telemetry_id') != '';