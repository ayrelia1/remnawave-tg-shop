-- Purchase mix, actual discounted prices. Historical NULL device tiers stay unknown.
-- A buyer can occur in several tariffs/months; do not add buyer counts across rows.
-- title: 05 Тарифы и сроки покупок
WITH cfg AS (
    SELECT CURRENT_TIMESTAMP AS as_of
), paid AS (
    SELECT p.user_id, p.hwid_device_limit AS devices,
        p.subscription_duration_months AS duration,
        p.base_amount::numeric AS revenue, p.created_at,
        DATE_TRUNC('month', p.created_at AT TIME ZONE 'Europe/Moscow') AS month
    FROM payments p CROSS JOIN cfg c
    WHERE p.status = 'succeeded' AND p.amount > 0
      AND (p.created_at <= c.as_of OR p.created_at IS NULL)
), scoped AS (
    SELECT 0 AS scope, NULL::timestamp AS period_month, p.* FROM paid p
    UNION ALL
    SELECT 1 AS scope, p.month AS period_month, p.* FROM paid p WHERE p.created_at IS NOT NULL
), grouped AS (
    SELECT scope, period_month, devices, duration,
        COUNT(DISTINCT user_id) AS buyers, COUNT(*) AS purchases,
        COUNT(revenue) AS valued, COUNT(*) FILTER (WHERE revenue IS NULL) AS unvalued,
        COALESCE(SUM(revenue), 0) AS revenue, AVG(revenue) AS average_ticket
    FROM scoped GROUP BY scope, period_month, devices, duration
)
SELECT CASE WHEN scope = 0 THEN 'За всё время' ELSE TO_CHAR(period_month, 'YYYY-MM') END AS "Период",
    CASE WHEN devices IS NULL THEN 'Не записан'
         WHEN devices = 0 THEN 'Без лимита' ELSE devices::text END AS "Устройств",
    duration AS "Срок, мес.",
    buyers AS "Покупателей",
    purchases AS "Покупок",
    valued AS "Покупок с оценкой",
    ROUND(revenue, 2) AS "Выручка, ₽",
    ROUND(average_ticket, 2) AS "Средний чек, ₽",
    CASE WHEN duration > 0 THEN ROUND(average_ticket / duration, 2) END AS "Цена месяца, ₽",
    ROUND(100.0 * revenue / NULLIF(SUM(revenue) OVER (PARTITION BY scope, period_month), 0), 2)
        AS "Доля выручки, %",
    unvalued AS "Оплат без курса"
FROM grouped
ORDER BY scope, period_month, devices NULLS FIRST, duration NULLS FIRST;
