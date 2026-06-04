-- =============================================================================
-- ClickHouse MaterializedView — витрина отчётности report_mart
-- =============================================================================
-- report_mart_mv автоматически перестраивает витрину при каждом изменении
-- customers_cdc или telemetry_cdc через CDC.
--
-- В отличие от batch-ETL, MV обновляется инкрементально: при поступлении
-- новых строк через KafkaEngine MV агрегирует их и вставляет в report_mart.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. Целевая таблица: report_mart_cdc
--    AggregatingMergeTree обеспечивает инкрементальную агрегацию.
--    При вставке строк с одинаковым ключом (customer_id, report_period_start)
--    ClickHouse автоматически пересчитывает агрегаты.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS report_mart_cdc
(
    customer_id          String,
    full_name            AggregateFunction(argMax, String, DateTime),
    email                AggregateFunction(argMax, String, DateTime),
    phone                AggregateFunction(argMax, String, DateTime),
    registration_date    AggregateFunction(argMax, String, DateTime),
    tariff_plan          AggregateFunction(argMax, String, DateTime),
    status               AggregateFunction(argMax, String, DateTime),
    total_measurements   AggregateFunction(count, UInt64),
    device_types         AggregateFunction(groupUniqArray, String),
    metrics_collected    AggregateFunction(groupUniqArray, String),
    avg_battery_level    AggregateFunction(avg, Float64),
    max_signal_strength  AggregateFunction(max, Float64),
    first_measurement_ts AggregateFunction(min, DateTime),
    last_measurement_ts  AggregateFunction(max, DateTime),
    report_generated_at  AggregateFunction(max, DateTime),
    report_period_start  DateTime,
    report_period_end    AggregateFunction(max, DateTime)
)
ENGINE = AggregatingMergeTree()
ORDER BY (customer_id, report_period_start)
SETTINGS index_granularity = 8192;

-- ---------------------------------------------------------------------------
-- 2. MaterializedView: report_mart_cdc_mv
--    Связывает customers_cdc и telemetry_cdc, создавая денормализованную
--    витрину с агрегацией по каждому клиенту.
--    Использует State-функции для совместимости с AggregatingMergeTree.
-- ---------------------------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS report_mart_cdc_mv
TO report_mart_cdc
AS
SELECT
    c.customer_id,
    argMaxState(c.full_name,         now())          AS full_name,
    argMaxState(c.email,             now())          AS email,
    argMaxState(c.phone,             now())          AS phone,
    argMaxState(c.registration_date, now())          AS registration_date,
    argMaxState(c.tariff_plan,       now())          AS tariff_plan,
    argMaxState(c.status,            now())          AS status,
    countState(t.telemetry_id)                       AS total_measurements,
    groupUniqArrayState(t.device_type)               AS device_types,
    groupUniqArrayState(t.metric_name)               AS metrics_collected,
    avgState(if(t.metric_name = 'battery_level',  t.metric_value, NULL)) AS avg_battery_level,
    maxState(if(t.metric_name = 'signal_strength', t.metric_value, NULL)) AS max_signal_strength,
    minState(t.timestamp)                            AS first_measurement_ts,
    maxState(t.timestamp)                            AS last_measurement_ts,
    maxState(now())                                  AS report_generated_at,
    toDateTime('2026-01-01 00:00:00', 'UTC')         AS report_period_start,
    maxState(now())                                  AS report_period_end
FROM customers_cdc AS c
LEFT JOIN telemetry_cdc AS t
    ON c.customer_id = t.customer_id
WHERE c.customer_id != ''
GROUP BY
    c.customer_id,
    report_period_start;

-- ---------------------------------------------------------------------------
-- 3. Представление для удобного чтения (разворачивает State-функции)
--    reports-api должен запрашивать это представление с модификатором FINAL.
-- ---------------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS report_mart AS
SELECT
    customer_id,
    argMaxMerge(full_name)             AS full_name,
    argMaxMerge(email)                 AS email,
    argMaxMerge(phone)                 AS phone,
    argMaxMerge(registration_date)     AS registration_date,
    argMaxMerge(tariff_plan)           AS tariff_plan,
    argMaxMerge(status)                AS status,
    countMerge(total_measurements)     AS total_measurements,
    arrayStringConcat(
        arraySort(groupUniqArrayMerge(device_types)), ', '
    )                                   AS device_types,
    arrayStringConcat(
        arraySort(groupUniqArrayMerge(metrics_collected)), ', '
    )                                   AS metrics_collected,
    avgMerge(avg_battery_level)        AS avg_battery_level,
    maxMerge(max_signal_strength)      AS max_signal_strength,
    minMerge(first_measurement_ts)     AS first_measurement_ts,
    maxMerge(last_measurement_ts)      AS last_measurement_ts,
    maxMerge(report_generated_at)      AS report_generated_at,
    report_period_start,
    maxMerge(report_period_end)        AS report_period_end
FROM report_mart_cdc
GROUP BY
    customer_id,
    report_period_start;