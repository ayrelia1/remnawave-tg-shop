-- Groups are defined by registration month. Non-payers are included.
-- Fixed-age windows include only users with a FULL observation window.
-- Current activity is a snapshot, not historical retention at the window end.
-- title: 03 По месяцу регистрации
WITH cfg AS (
    SELECT CURRENT_TIMESTAMP AS as_of
), horizons AS (
    SELECT * FROM (VALUES
        (30, INTERVAL '720 hours'), (90, INTERVAL '2160 hours'),
        (180, INTERVAL '4320 hours'), (400, INTERVAL '9600 hours')
    ) AS h(days, observation_window)
), paid AS (
    SELECT p.* FROM payments p CROSS JOIN cfg c
    WHERE p.status = 'succeeded' AND (p.created_at <= c.as_of OR p.created_at IS NULL)
), active AS (
    SELECT DISTINCT s.user_id FROM subscriptions s CROSS JOIN cfg c
    WHERE s.is_active IS TRUE AND s.end_date > c.as_of
      AND s.provider IS NOT NULL AND s.duration_months > 0
), per_user AS (
    SELECT u.user_id, u.registration_date, h.days,
        DATE_TRUNC('month', u.registration_date AT TIME ZONE 'Europe/Moscow') AS cohort,
        u.registration_date + h.observation_window <= c.as_of AS mature,
        COUNT(p.payment_id) FILTER (WHERE p.amount > 0) > 0 AS payer,
        COALESCE(SUM(p.base_amount::numeric), 0) AS lifetime_revenue,
        a.user_id IS NOT NULL AND COUNT(p.payment_id) FILTER (WHERE p.amount > 0) > 0 AS active_payer,
        COUNT(p.payment_id) FILTER (WHERE p.base_amount IS NULL) AS unvalued,
        CASE WHEN u.registration_date + h.observation_window <= c.as_of THEN
            COALESCE(SUM(p.base_amount::numeric) FILTER (
                WHERE p.created_at >= u.registration_date
                  AND p.created_at < u.registration_date + h.observation_window
            ), 0)
        END AS revenue_in_window,
        CASE WHEN u.registration_date + h.observation_window <= c.as_of THEN
            CASE WHEN COUNT(p.payment_id) FILTER (
                WHERE p.amount > 0 AND p.created_at >= u.registration_date
                  AND p.created_at < u.registration_date + h.observation_window
            ) > 0 THEN 1 ELSE 0 END
        END AS converted_in_window
    FROM users u CROSS JOIN cfg c CROSS JOIN horizons h
    LEFT JOIN paid p ON p.user_id = u.user_id
    LEFT JOIN active a ON a.user_id = u.user_id
    WHERE u.registration_date <= c.as_of
    GROUP BY u.user_id, u.registration_date, h.days, h.observation_window, c.as_of, a.user_id
)
SELECT cohort::date AS "Месяц регистрации",
    days AS "Окно, дней",
    COUNT(*) AS "Пользователей в группе",
    COUNT(*) FILTER (WHERE payer) AS "Платили к сегодня",
    ROUND(100.0 * COUNT(*) FILTER (WHERE payer) / NULLIF(COUNT(*), 0), 2) AS "Конверсия к сегодня, %",
    ROUND(SUM(lifetime_revenue), 2) AS "Выручка к сегодня, ₽",
    ROUND(AVG(lifetime_revenue), 2) AS "На юзера к сегодня, ₽",
    ROUND(AVG(lifetime_revenue) FILTER (WHERE payer), 2) AS "LTV платившего, ₽",
    COUNT(*) FILTER (WHERE mature) AS "С полным окном",
    ROUND(100.0 * AVG(converted_in_window), 2) AS "Конверсия за окно, %",
    ROUND(AVG(revenue_in_window), 2) AS "На юзера за окно, ₽",
    ROUND(100.0 * COUNT(*) FILTER (WHERE active_payer)
        / NULLIF(COUNT(*) FILTER (WHERE payer), 0), 2) AS "Платившие активны, %",
    SUM(unvalued) AS "Оплат без курса"
FROM per_user
GROUP BY cohort, days
ORDER BY cohort, days;
