-- PostgreSQL / DBeaver: six independent read-only reports.
-- Execute this entire file as a SQL script, or execute an entire numbered file.
-- All money is known base_amount in RUB; run the data-quality result first.
-- FILE: 01_overview.sql
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
-- FILE: 02_monthly_payments.sql
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
-- FILE: 03_registration_cohorts.sql
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
-- FILE: 04_first_payment_cohorts.sql
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
-- FILE: 05_tariff_mix.sql
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
-- FILE: 06_data_quality.sql
-- Review these checks before interpreting financial totals and age-based cohorts.
-- The 1:1 non-RUB flag requires review, not automatic revaluation.
-- title: 06 Проверка данных
WITH cfg AS (
    SELECT CURRENT_TIMESTAMP AS as_of
), paid AS (
    SELECT p.* FROM payments p WHERE p.status = 'succeeded'
), active AS (
    SELECT s.* FROM subscriptions s CROSS JOIN cfg c
    WHERE s.is_active IS TRUE AND s.end_date > c.as_of
), duplicate_active AS (
    SELECT user_id FROM active GROUP BY user_id HAVING COUNT(*) > 1
)
SELECT v.check_name AS "Проверка", v.value AS "Количество"
FROM cfg c
CROSS JOIN LATERAL (VALUES
    (1, 'Успешных платежей без base_amount', (SELECT COUNT(*) FROM paid WHERE base_amount IS NULL)),
    (2, 'Не-RUB платежей с курсом 1:1: проверить миграцию',
        (SELECT COUNT(*) FROM paid WHERE UPPER(currency) <> 'RUB' AND fx_rate = 1)),
    (3, 'Успешных платежей без положительного срока',
        (SELECT COUNT(*) FROM paid WHERE subscription_duration_months IS NULL OR subscription_duration_months <= 0)),
    (4, 'Успешных платежей без даты создания', (SELECT COUNT(*) FROM paid WHERE created_at IS NULL)),
    (5, 'Успешных платежей с будущей датой', (SELECT COUNT(*) FROM paid WHERE created_at > c.as_of)),
    (6, 'Пользователей без даты регистрации', (SELECT COUNT(*) FROM users WHERE registration_date IS NULL)),
    (7, 'Пользователей с будущей регистрацией', (SELECT COUNT(*) FROM users WHERE registration_date > c.as_of)),
    (8, 'Платежей с датой раньше регистрации пользователя',
        (SELECT COUNT(*) FROM paid p JOIN users u ON u.user_id = p.user_id WHERE p.created_at < u.registration_date)),
    (9, 'Успешных платежей с отрицательной суммой',
        (SELECT COUNT(*) FROM paid WHERE amount < 0 OR base_amount < 0)),
    (10, 'Успешных платежей без записанного тарифа устройств',
        (SELECT COUNT(*) FROM paid WHERE hwid_device_limit IS NULL)),
    (11, 'Пользователей с несколькими активными подписками', (SELECT COUNT(*) FROM duplicate_active)),
    (12, 'Активных платных подписок без подтверждённых покупок',
        (SELECT COUNT(*) FROM active s WHERE s.provider IS NOT NULL AND s.duration_months > 0
            AND NOT EXISTS (SELECT 1 FROM paid p WHERE p.user_id = s.user_id AND p.amount > 0
                AND (p.created_at <= c.as_of OR p.created_at IS NULL)))),
    (13, 'Заблокированных с действующей подпиской',
        (SELECT COUNT(DISTINCT s.user_id) FROM active s JOIN users u ON u.user_id = s.user_id WHERE u.is_banned IS TRUE))
) AS v(sort_order, check_name, value)
ORDER BY v.sort_order;
