-- Cash-style collections by creation month of succeeded payments, Moscow time.
-- No historical active subscriber counts/MRR/churn are inferred from snapshots.
-- title: 02 Оплаты по месяцам
WITH cfg AS (
    SELECT CURRENT_TIMESTAMP AS as_of,
        DATE_TRUNC('month', CURRENT_TIMESTAMP AT TIME ZONE 'Europe/Moscow') AS current_month
), paid AS (
    SELECT p.payment_id, p.user_id, p.amount, p.base_amount::numeric AS revenue,
        p.subscription_duration_months AS duration,
        DATE_TRUNC('month', p.created_at AT TIME ZONE 'Europe/Moscow') AS month,
        p.created_at
    FROM payments p CROSS JOIN cfg c
    WHERE p.status = 'succeeded' AND p.created_at <= c.as_of
), first_paid AS (
    SELECT user_id, MIN(created_at) AS first_paid_at
    FROM paid WHERE amount > 0 GROUP BY user_id
), registrations AS (
    SELECT DATE_TRUNC('month', u.registration_date AT TIME ZONE 'Europe/Moscow') AS month,
        COUNT(*) AS new_users
    FROM users u CROSS JOIN cfg c
    WHERE u.registration_date <= c.as_of GROUP BY 1
), bounds AS (
    SELECT COALESCE(MIN(month), (SELECT current_month FROM cfg)) AS first_month
    FROM (SELECT month FROM paid UNION ALL SELECT month FROM registrations) d
), months AS (
    SELECT g.month FROM bounds b CROSS JOIN cfg c
    CROSS JOIN LATERAL GENERATE_SERIES(b.first_month, c.current_month, INTERVAL '1 month') AS g(month)
), monthly_money AS (
    SELECT p.month, COUNT(*) AS payments,
        COUNT(DISTINCT p.user_id) FILTER (WHERE p.amount > 0) AS payers,
        COUNT(DISTINCT p.user_id) FILTER (
            WHERE p.amount > 0
              AND DATE_TRUNC('month', f.first_paid_at AT TIME ZONE 'Europe/Moscow') = p.month
        ) AS new_payers,
        COALESCE(SUM(p.revenue), 0) AS revenue,
        COUNT(*) FILTER (WHERE p.revenue IS NULL) AS unvalued,
        AVG(p.duration::numeric) FILTER (WHERE p.amount > 0 AND p.duration > 0) AS average_term,
        100.0 * COUNT(*) FILTER (WHERE p.amount > 0 AND p.duration >= 6)
            / NULLIF(COUNT(*) FILTER (WHERE p.amount > 0 AND p.duration > 0), 0) AS long_share
    FROM paid p LEFT JOIN first_paid f ON f.user_id = p.user_id GROUP BY p.month
), buyer_rates AS (
    SELECT month, user_id,
        SUM(revenue) FILTER (WHERE amount > 0 AND duration > 0)
            / NULLIF(SUM(duration) FILTER (
                WHERE amount > 0 AND duration > 0 AND revenue IS NOT NULL
            ), 0) AS monthly_price
    FROM paid GROUP BY month, user_id
), monthly_rates AS (
    SELECT month, AVG(monthly_price) AS monthly_price FROM buyer_rates GROUP BY month
), monthly AS (
    SELECT m.month, COALESCE(r.new_users, 0) AS new_users,
        COALESCE(mm.payments, 0) AS payments, COALESCE(mm.payers, 0) AS payers,
        COALESCE(mm.new_payers, 0) AS new_payers,
        COALESCE(mm.revenue, 0) AS revenue, COALESCE(mm.unvalued, 0) AS unvalued,
        mm.average_term, mm.long_share, mr.monthly_price
    FROM months m
    LEFT JOIN registrations r ON r.month = m.month
    LEFT JOIN monthly_money mm ON mm.month = m.month
    LEFT JOIN monthly_rates mr ON mr.month = m.month
), enriched AS (
    SELECT m.*, SUM(new_users) OVER (ORDER BY month) AS registered_base,
        LAG(revenue) OVER (ORDER BY month) AS previous_revenue
    FROM monthly m
)
SELECT m.month::date AS "Месяц",
    CASE WHEN m.month = c.current_month THEN 'Неполный' ELSE 'Закрыт' END AS "Период",
    m.new_users AS "Новых регистраций",
    m.registered_base AS "Зарегистрировано к концу",
    m.payments AS "Успешных оплат",
    m.payers AS "Плативших в месяце",
    m.new_payers AS "Впервые плативших",
    m.payers - m.new_payers AS "Вернувшихся платящих",
    ROUND(m.revenue, 2) AS "Выручка, ₽",
    CASE WHEN m.month < c.current_month THEN
        ROUND(100.0 * (m.revenue - m.previous_revenue) / NULLIF(m.previous_revenue, 0), 2)
    END AS "Рост выручки, %",
    ROUND(m.revenue / NULLIF(m.payers, 0), 2) AS "На платившего, ₽",
    ROUND(m.revenue / NULLIF(m.registered_base, 0), 2) AS "На юзера базы, ₽",
    ROUND(m.monthly_price, 2) AS "Мес. цена покупок, ₽",
    ROUND(m.average_term, 2) AS "Срок покупки, мес.",
    ROUND(m.long_share, 2) AS "Покупок на 6+ мес., %",
    m.unvalued AS "Оплат без курса"
FROM enriched m CROSS JOIN cfg c
ORDER BY m.month;
