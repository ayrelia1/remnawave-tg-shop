-- Groups are first positive payment month AND first purchased duration.
-- Never treat no second payment within 30 days as churn for annual subscribers.
-- Compare equal horizons and first terms. Immature windows return NULL, not zero.
-- title: 04 LTV и повторные покупки
WITH cfg AS (
    SELECT CURRENT_TIMESTAMP AS as_of
), horizons AS (
    SELECT * FROM (VALUES
        (30, INTERVAL '720 hours'), (90, INTERVAL '2160 hours'),
        (180, INTERVAL '4320 hours'), (400, INTERVAL '9600 hours')
    ) AS h(days, observation_window)
), paid AS (
    SELECT p.* FROM payments p CROSS JOIN cfg c
    WHERE p.status = 'succeeded' AND p.created_at <= c.as_of
), ranked AS (
    SELECT p.*, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY created_at, payment_id) AS purchase_number
    FROM paid p WHERE amount > 0
), first_paid AS (
    SELECT user_id, created_at AS first_paid_at, subscription_duration_months AS first_term
    FROM ranked WHERE purchase_number = 1
), active AS (
    SELECT DISTINCT s.user_id FROM subscriptions s CROSS JOIN cfg c
    WHERE s.is_active IS TRUE AND s.end_date > c.as_of
      AND s.provider IS NOT NULL AND s.duration_months > 0
), per_user AS (
    SELECT f.user_id, f.first_term, h.days,
        DATE_TRUNC('month', f.first_paid_at AT TIME ZONE 'Europe/Moscow') AS cohort,
        f.first_paid_at + h.observation_window <= c.as_of AS mature,
        a.user_id IS NOT NULL AS active_payer,
        COALESCE(SUM(p.base_amount::numeric), 0) AS lifetime_revenue,
        CASE WHEN f.first_paid_at + h.observation_window <= c.as_of THEN
            COALESCE(SUM(p.base_amount::numeric) FILTER (
                WHERE p.created_at < f.first_paid_at + h.observation_window
            ), 0)
        END AS revenue_in_window,
        CASE WHEN f.first_paid_at + h.observation_window <= c.as_of THEN
            CASE WHEN COUNT(p.payment_id) FILTER (
                WHERE p.amount > 0 AND p.created_at < f.first_paid_at + h.observation_window
            ) >= 2 THEN 1 ELSE 0 END
        END AS repeated_in_window,
        CASE WHEN f.first_paid_at + h.observation_window <= c.as_of THEN
            COUNT(p.payment_id) FILTER (
                WHERE p.base_amount IS NULL AND p.created_at < f.first_paid_at + h.observation_window
            )
        END AS unvalued_in_window
    FROM first_paid f CROSS JOIN cfg c CROSS JOIN horizons h
    JOIN paid p ON p.user_id = f.user_id AND p.created_at >= f.first_paid_at
    LEFT JOIN active a ON a.user_id = f.user_id
    GROUP BY f.user_id, f.first_paid_at, f.first_term, h.days, h.observation_window, c.as_of, a.user_id
)
SELECT cohort::date AS "Месяц первой оплаты",
    first_term AS "Первый срок, мес.",
    days AS "Окно, дней",
    COUNT(*) AS "Платящих в группе",
    COUNT(*) FILTER (WHERE mature) AS "С полным окном",
    ROUND(100.0 * COUNT(*) FILTER (WHERE mature) / NULLIF(COUNT(*), 0), 2) AS "Полное окно, %",
    ROUND(AVG(lifetime_revenue), 2) AS "LTV к сегодня, ₽",
    ROUND(AVG(revenue_in_window), 2) AS "LTV за окно, ₽",
    ROUND(100.0 * AVG(repeated_in_window), 2) AS "Повтор за окно, %",
    ROUND(100.0 * COUNT(*) FILTER (WHERE active_payer) / NULLIF(COUNT(*), 0), 2) AS "Активны сегодня, %",
    SUM(unvalued_in_window) AS "Без курса за окно"
FROM per_user
GROUP BY cohort, first_term, days
ORDER BY cohort, first_term NULLS FIRST, days;
