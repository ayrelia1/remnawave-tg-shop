-- Current snapshot. Monthly spend is historical paid money / purchased months
-- per user, averaged across current active payers; it is not exact current MRR.
-- title: 01 Общая сводка
WITH cfg AS (
    SELECT CURRENT_TIMESTAMP AS as_of
), paid AS (
    SELECT p.* FROM payments p CROSS JOIN cfg c
    WHERE p.status = 'succeeded' AND (p.created_at <= c.as_of OR p.created_at IS NULL)
), payment_totals AS (
    SELECT user_id,
        COALESCE(SUM(base_amount::numeric), 0) AS revenue,
        COUNT(*) FILTER (WHERE amount > 0) AS purchases,
        COUNT(*) FILTER (WHERE base_amount IS NULL) AS unvalued,
        SUM(base_amount::numeric) FILTER (
            WHERE amount > 0 AND subscription_duration_months > 0
        ) AS time_revenue,
        SUM(subscription_duration_months) FILTER (
            WHERE amount > 0 AND subscription_duration_months > 0 AND base_amount IS NOT NULL
        ) AS purchased_months
    FROM paid GROUP BY user_id
), active AS (
    SELECT DISTINCT s.user_id FROM subscriptions s CROSS JOIN cfg c
    WHERE s.is_active IS TRUE AND s.end_date > c.as_of
      AND s.provider IS NOT NULL AND s.duration_months > 0
), per_user AS (
    SELECT u.user_id, u.is_banned,
        COALESCE(p.revenue, 0) AS revenue,
        COALESCE(p.purchases, 0) AS purchases,
        COALESCE(p.unvalued, 0) AS unvalued,
        a.user_id IS NOT NULL AND COALESCE(p.purchases, 0) > 0 AS active_payer,
        p.time_revenue / NULLIF(p.purchased_months, 0) AS monthly_spend
    FROM users u
    LEFT JOIN payment_totals p ON p.user_id = u.user_id
    LEFT JOIN active a ON a.user_id = u.user_id
), totals AS (
    SELECT COUNT(*) AS users,
        COUNT(*) FILTER (WHERE purchases > 0) AS payers,
        COUNT(*) FILTER (WHERE active_payer) AS active_payers,
        COUNT(*) FILTER (WHERE active_payer AND monthly_spend IS NOT NULL) AS priced_active,
        COUNT(*) FILTER (WHERE purchases >= 2) AS repeat_payers,
        COALESCE(SUM(revenue), 0) AS revenue,
        AVG(revenue) AS lifetime_arpu,
        AVG(revenue) FILTER (WHERE purchases > 0) AS observed_ltv,
        (PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY revenue)
            FILTER (WHERE purchases > 0))::numeric AS median_ltv,
        AVG(revenue) FILTER (WHERE active_payer) AS active_ltv,
        AVG(monthly_spend) FILTER (WHERE active_payer) AS monthly_average,
        COALESCE(SUM(monthly_spend) FILTER (WHERE active_payer), 0) AS monthly_total,
        COALESCE(SUM(unvalued), 0) AS unvalued,
        COUNT(*) FILTER (WHERE active_payer AND is_banned IS TRUE) AS banned_active
    FROM per_user
)
SELECT v.metric AS "Показатель", ROUND(v.value, 2) AS "Значение"
FROM totals t
CROSS JOIN LATERAL (VALUES
    (1, 'Средний расход активного платящего в месяц, ₽', t.monthly_average),
    (2, 'Активных платящих пользователей', t.active_payers::numeric),
    (3, 'Активных в расчёте месячного расхода', t.priced_active::numeric),
    (4, 'Месячный эквивалент всех активных, оценка, ₽', t.monthly_total),
    (5, 'Накопленный LTV всех плативших, ₽', t.observed_ltv),
    (6, 'Медианный LTV всех плативших, ₽', t.median_ltv),
    (7, 'Накопленный LTV нынешних активных платящих, ₽', t.active_ltv),
    (8, 'Из всех плативших активны сейчас, %', 100.0 * t.active_payers / NULLIF(t.payers, 0)),
    (9, 'Плативших с двумя и более покупками, %', 100.0 * t.repeat_payers / NULLIF(t.payers, 0)),
    (10, 'Выручка за всё время по учтённым оплатам, ₽', t.revenue),
    (11, 'Выручка за всё время на зарегистрированного, ₽', t.lifetime_arpu),
    (12, 'Всего пользователей', t.users::numeric),
    (13, 'Пользователей с покупками', t.payers::numeric),
    (14, 'Из зарегистрированных платили, %', 100.0 * t.payers / NULLIF(t.users, 0)),
    (15, 'Платежей без валютной оценки', t.unvalued::numeric),
    (16, 'Активных платящих без месячной оценки', (t.active_payers - t.priced_active)::numeric),
    (17, 'Заблокированных среди активных платящих', t.banned_active::numeric)
) AS v(sort_order, metric, value)
ORDER BY v.sort_order;
