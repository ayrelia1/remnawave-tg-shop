-- PostgreSQL / DBeaver. Run as a SQL script for six separate result sets.
-- Read-only: no database objects, settings or data are changed.
-- Schema: db/models.py, including migration 0007_payment_base_amount.
-- Money is in config.currency.BASE_CURRENCY, currently RUB.
-- Revenue means recorded succeeded payment value, before fees, tax and costs.
-- This schema does not provide a complete refund ledger or a paid_at timestamp.
-- Month attribution therefore uses payment.created_at, as the bot's reports do.
-- NULL base_amount is excluded from money totals and reported as missing.
-- Never replace missing base_amount with amount: currencies may differ.
-- Historical non-RUB payments were backfilled 1:1 by migration 0007; audit them.
-- Active = EXISTS(is_active IS TRUE AND end_date > CURRENT_TIMESTAMP).
-- Active includes trials, bonuses, banned accounts and cancellation grace periods
-- when those records satisfy that same predicate. Banned counts are shown below.
-- Do not filter start_date <= now: prepaid renewals can move it into the future.
-- Reports 3, 5 and purchased-month metrics assume time subscriptions only.
-- Traffic purchases overload subscription_duration_months with package size;
-- historical traffic sales must be identified separately before using these.
-- NULL device limits stay unknown/legacy; today's tariff must not rewrite history.


-- 1. Coverage audit. Resolve missing valuations before treating totals as complete.
-- A 1:1 non-RUB rate is a review flag, not proof of an error.
SELECT
    p.currency,
    p.provider,
    COUNT(*) AS succeeded_payments,
    COUNT(*) FILTER (WHERE p.amount > 0) AS positive_payments,
    COUNT(*) FILTER (WHERE p.base_amount IS NULL) AS unvalued_payments,
    COUNT(*) FILTER (
        WHERE UPPER(p.currency) <> 'RUB' AND p.fx_rate = 1
    ) AS non_rub_one_to_one_rate_payments,
    COUNT(*) FILTER (
        WHERE p.subscription_duration_months IS NULL
           OR p.subscription_duration_months <= 0
    ) AS payments_without_positive_duration,
    ROUND(SUM(p.base_amount::numeric), 2) AS known_revenue_rub
FROM payments p
WHERE p.status = 'succeeded'
GROUP BY p.currency, p.provider
ORDER BY p.currency, p.provider;


-- 2. Lifetime revenue per user, including users with no purchases.
-- active_users: ALL lifetime payments of users active NOW, including old tariffs.
-- ever_paid_users: users with at least one succeeded payment with amount > 0.
-- active_paid_users: active time subscription with a provider, plus payer history.
-- LTV here is observed cash revenue to date, not a future lifetime estimate.
-- payer_conversion_pct is lifetime conversion inside the selected population.
WITH payment_totals AS (
    SELECT
        p.user_id,
        COALESCE(SUM(p.base_amount::numeric), 0::numeric) AS revenue_rub,
        COUNT(*) FILTER (WHERE p.amount > 0) AS paid_payments,
        COUNT(*) FILTER (WHERE p.base_amount IS NULL) AS unvalued_payments
    FROM payments p
    WHERE p.status = 'succeeded'
    GROUP BY p.user_id
), user_metrics AS (
    SELECT
        u.user_id,
        u.is_banned,
        COALESCE(pt.revenue_rub, 0::numeric) AS revenue_rub,
        COALESCE(pt.paid_payments, 0) AS paid_payments,
        COALESCE(pt.unvalued_payments, 0) AS unvalued_payments,
        EXISTS (
            SELECT 1 FROM subscriptions s
            WHERE s.user_id = u.user_id
              AND s.is_active IS TRUE
              AND s.end_date > CURRENT_TIMESTAMP
        ) AS is_active,
        EXISTS (
            SELECT 1 FROM subscriptions s
            WHERE s.user_id = u.user_id
              AND s.is_active IS TRUE
              AND s.end_date > CURRENT_TIMESTAMP
              AND s.provider IS NOT NULL
              AND s.duration_months > 0
        ) AS has_active_paid_subscription
    FROM users u
    LEFT JOIN payment_totals pt ON pt.user_id = u.user_id
), segments AS (
    SELECT um.*, pop.segment_order, pop.segment
    FROM user_metrics um
    CROSS JOIN LATERAL (VALUES
        (1, 'all_users', TRUE),
        (2, 'active_users', um.is_active),
        (3, 'ever_paid_users', um.paid_payments > 0),
        (4, 'active_paid_users',
            um.has_active_paid_subscription AND um.paid_payments > 0)
    ) AS pop(segment_order, segment, include_user)
    WHERE pop.include_user
)
SELECT
    pop.segment,
    COUNT(user_id) AS users,
    COUNT(*) FILTER (WHERE paid_payments > 0) AS paying_users,
    COUNT(*) FILTER (WHERE is_banned IS TRUE) AS banned_users,
    ROUND(COALESCE(SUM(revenue_rub), 0::numeric), 2) AS known_lifetime_revenue_rub,
    ROUND(AVG(revenue_rub), 2) AS lifetime_arpu_rub,
    ROUND(AVG(revenue_rub) FILTER (
        WHERE paid_payments > 0
    ), 2) AS observed_payer_ltv_rub,
    ROUND((PERCENTILE_CONT(0.5) WITHIN GROUP (
        ORDER BY revenue_rub
    ) FILTER (WHERE paid_payments > 0))::numeric, 2) AS median_payer_ltv_rub,
    ROUND(100.0 * COUNT(*) FILTER (WHERE paid_payments > 0)
        / NULLIF(COUNT(user_id), 0), 2) AS payer_conversion_pct,
    ROUND(100.0 * COUNT(*) FILTER (WHERE paid_payments >= 2)
        / NULLIF(COUNT(*) FILTER (WHERE paid_payments > 0), 0), 2)
        AS repeat_payers_pct,
    ROUND(AVG(paid_payments) FILTER (
        WHERE paid_payments > 0
    ), 2) AS payments_per_payer,
    COALESCE(SUM(unvalued_payments), 0) AS unvalued_payments
FROM (VALUES
    (1, 'all_users'), (2, 'active_users'),
    (3, 'ever_paid_users'), (4, 'active_paid_users')
) AS pop(segment_order, segment)
LEFT JOIN segments seg ON seg.segment_order = pop.segment_order
GROUP BY pop.segment_order, pop.segment
ORDER BY pop.segment_order;


-- 3. Actual purchase mix by device tier AND purchased duration.
-- A user who bought multiple tariffs belongs to multiple rows: do not add buyers.
-- The monthly rate is the actual discounted ticket divided by its months.
-- Current configured prices and subscription snapshots are not used here.
WITH purchase_metrics AS (
    SELECT p.*,
        EXISTS (
            SELECT 1 FROM subscriptions s
            WHERE s.user_id = p.user_id
              AND s.is_active IS TRUE
              AND s.end_date > CURRENT_TIMESTAMP
        ) AS is_active_user
    FROM payments p
    WHERE p.status = 'succeeded' AND p.amount > 0
), segments AS (
    SELECT pm.*, pop.segment_order, pop.segment
    FROM purchase_metrics pm
    CROSS JOIN LATERAL (VALUES
        (1, 'all_purchases', TRUE),
        (2, 'active_users_purchases', pm.is_active_user)
    ) AS pop(segment_order, segment, include_payment)
    WHERE pop.include_payment
)
SELECT
    segment,
    hwid_device_limit,
    subscription_duration_months AS purchased_months,
    COUNT(DISTINCT user_id) AS buyers,
    COUNT(*) AS payments,
    COUNT(base_amount) AS valued_payments,
    COUNT(*) FILTER (WHERE base_amount IS NULL) AS unvalued_payments,
    ROUND(SUM(base_amount::numeric), 2) AS known_revenue_rub,
    ROUND(AVG(base_amount::numeric), 2) AS average_ticket_rub,
    CASE WHEN subscription_duration_months > 0 THEN
        ROUND(AVG(base_amount::numeric) / subscription_duration_months, 2)
    END AS average_price_per_purchased_month_rub
FROM segments
GROUP BY segment_order, segment, hwid_device_limit, subscription_duration_months
ORDER BY segment_order, hwid_device_limit NULLS FIRST,
    subscription_duration_months NULLS FIRST;


-- 4. Recorded payments by calendar month in Moscow, including zero-sale months.
-- History begins in the month of the first succeeded payment.
-- Averages exclude the current incomplete month; the first sale month is included.
-- Recent average uses up to the last three CLOSED months available in that history.
-- These are cash-style collections by payment creation date, not revenue recognition.
WITH params AS (
    SELECT DATE_TRUNC('month', CURRENT_TIMESTAMP AT TIME ZONE 'Europe/Moscow')
        AS current_month
), paid AS (
    SELECT
        p.user_id,
        p.amount,
        p.base_amount::numeric AS revenue_rub,
        DATE_TRUNC('month', p.created_at AT TIME ZONE 'Europe/Moscow') AS month
    FROM payments p
    WHERE p.status = 'succeeded'
), bounds AS (
    SELECT COALESCE(MIN(paid.month), (SELECT current_month FROM params)) AS first_month
    FROM paid
), months AS (
    SELECT g.month
    FROM bounds b CROSS JOIN params cfg
    CROSS JOIN LATERAL GENERATE_SERIES(
        b.first_month, cfg.current_month, INTERVAL '1 month'
    ) AS g(month)
), monthly AS (
    SELECT
        m.month,
        COUNT(p.user_id) AS succeeded_payments,
        COUNT(DISTINCT p.user_id) FILTER (WHERE p.amount > 0) AS paying_users,
        COUNT(p.user_id) FILTER (WHERE p.revenue_rub IS NULL) AS unvalued_payments,
        COALESCE(SUM(p.revenue_rub), 0::numeric) AS revenue_rub
    FROM months m
    LEFT JOIN paid p ON p.month = m.month
    GROUP BY m.month
)
SELECT
    m.month::date AS month,
    m.month = cfg.current_month AS is_incomplete_month,
    m.succeeded_payments,
    m.paying_users,
    m.unvalued_payments,
    ROUND(m.revenue_rub, 2) AS known_collections_rub,
    ROUND(m.revenue_rub / NULLIF(m.paying_users, 0), 2) AS monthly_arppu_rub,
    ROUND(AVG(m.revenue_rub) FILTER (
        WHERE m.month < cfg.current_month
    ) OVER (), 2) AS average_closed_month_collections_rub,
    ROUND(AVG(m.revenue_rub) FILTER (
        WHERE m.month >= cfg.current_month - INTERVAL '3 months'
          AND m.month < cfg.current_month
    ) OVER (), 2) AS average_recent_closed_month_collections_rub
FROM monthly m CROSS JOIN params cfg
ORDER BY m.month;


-- 5. Current monthly revenue equivalent + next 12 full calendar months of renewals.
-- MRR estimate = SUM(latest positive purchase base_amount / purchased months)
-- for currently active paid time subscriptions that can be matched unambiguously.
-- One row per active user; overlapping active subscription rows are not priced.
-- Last payment must match both current duration and recorded device tier.
-- This is a proxy: prepayments, tariff conversions, gifts and bonus days mean
-- it is NOT an exact allocation of historical payments over service intervals.
-- subscription.start_date is a mutable snapshot, not a service-period ledger.
--
-- Renewal scenario: no new customers, same latest actual paid price/duration,
-- renewal exactly at expiry, no future bonuses, tariff switches or price changes.
-- Current expiry already includes recorded prepaid extensions and bonus days.
-- conditional_renewal_probability is per renewal, not monthly churn.
-- Set that probability between 0.00 and 1.00 inclusive.
-- Default 1.00 = 100% retention scenario, NOT an estimated retention rate.
-- For 0.80, each successive renewal is weighted 0.80, 0.64, 0.512, etc.
-- Estimates exclude subscriptions without an unambiguous priced match.
-- No future cash is added for payments already received.
WITH RECURSIVE params AS (
    SELECT
        CURRENT_TIMESTAMP AS as_of,
        DATE_TRUNC('month', CURRENT_TIMESTAMP AT TIME ZONE 'Europe/Moscow')
            + INTERVAL '1 month' AS first_forecast_month,
        DATE_TRUNC('month', CURRENT_TIMESTAMP AT TIME ZONE 'Europe/Moscow')
            + INTERVAL '13 months' AS forecast_end,
        1.00::numeric AS conditional_renewal_probability
), active_candidates AS (
    SELECT s.*, COUNT(*) OVER (PARTITION BY s.user_id) AS active_subscription_rows
    FROM subscriptions s
    JOIN users u ON u.user_id = s.user_id
    CROSS JOIN params cfg
    WHERE s.is_active IS TRUE AND s.end_date > cfg.as_of
), active_subscriptions AS (
    SELECT DISTINCT ON (user_id) *
    FROM active_candidates
    ORDER BY user_id, end_date DESC, subscription_id DESC
), latest_payments AS (
    SELECT DISTINCT ON (p.user_id) p.*
    FROM payments p
    WHERE p.status = 'succeeded' AND p.amount > 0
    ORDER BY p.user_id, p.created_at DESC NULLS LAST, p.payment_id DESC
), rates AS (
    SELECT
        s.user_id,
        s.end_date AT TIME ZONE 'Europe/Moscow' AS expires_at_local,
        s.provider IS NOT NULL AND s.duration_months > 0 AS is_paid_subscription,
        p.subscription_duration_months AS renewal_months,
        CASE WHEN s.active_subscription_rows = 1
            AND s.provider IS NOT NULL AND s.duration_months > 0
            AND p.base_amount > 0
            AND p.subscription_duration_months = s.duration_months
            AND p.hwid_device_limit IS NOT DISTINCT FROM s.hwid_device_limit
        THEN p.base_amount::numeric END AS renewal_amount_rub
    FROM active_subscriptions s
    LEFT JOIN latest_payments p ON p.user_id = s.user_id
), current_metrics AS (
    SELECT
        COUNT(*) AS active_users_now,
        COUNT(*) FILTER (WHERE renewal_amount_rub IS NOT NULL)
            AS active_paid_users_with_estimate,
        COUNT(*) FILTER (
            WHERE is_paid_subscription AND renewal_amount_rub IS NULL
        ) AS active_paid_users_without_estimate,
        COALESCE(SUM(renewal_amount_rub / NULLIF(renewal_months, 0)), 0::numeric)
            AS estimated_mrr_rub
    FROM rates
), renewals AS (
    SELECT
        r.user_id, r.expires_at_local AS renewal_at_local,
        r.renewal_months, r.renewal_amount_rub, 1 AS renewal_number
    FROM rates r
    WHERE r.renewal_amount_rub IS NOT NULL
    UNION ALL
    SELECT
        r.user_id,
        r.renewal_at_local + MAKE_INTERVAL(months => r.renewal_months),
        r.renewal_months, r.renewal_amount_rub, r.renewal_number + 1
    FROM renewals r CROSS JOIN params cfg
    WHERE r.renewal_at_local < cfg.forecast_end AND r.renewal_number < 13
), forecast AS (
    SELECT
        DATE_TRUNC('month', r.renewal_at_local) AS month,
        COUNT(DISTINCT r.user_id) AS due_users_if_all_renew,
        SUM(r.renewal_amount_rub * POWER(
            cfg.conditional_renewal_probability, r.renewal_number
        )) AS scenario_collections_rub
    FROM renewals r CROSS JOIN params cfg
    WHERE r.renewal_at_local >= cfg.first_forecast_month
      AND r.renewal_at_local < cfg.forecast_end
    GROUP BY DATE_TRUNC('month', r.renewal_at_local)
), months AS (
    SELECT g.month
    FROM params cfg
    CROSS JOIN LATERAL GENERATE_SERIES(
        cfg.first_forecast_month, cfg.forecast_end - INTERVAL '1 month',
        INTERVAL '1 month'
    ) AS g(month)
)
SELECT
    m.month::date AS month,
    ROUND(100 * cfg.conditional_renewal_probability, 2) AS renewal_probability_pct,
    cm.active_users_now,
    cm.active_paid_users_with_estimate,
    cm.active_paid_users_without_estimate,
    ROUND(cm.estimated_mrr_rub, 2) AS latest_purchase_mrr_estimate_rub,
    ROUND(cm.estimated_mrr_rub
        / NULLIF(cm.active_paid_users_with_estimate, 0), 2)
        AS estimated_monthly_arppu_rub,
    COALESCE(f.due_users_if_all_renew, 0) AS due_users_if_all_renew,
    ROUND(COALESCE(f.scenario_collections_rub, 0::numeric), 2)
        AS scenario_renewal_collections_rub,
    ROUND(AVG(COALESCE(f.scenario_collections_rub, 0::numeric)) OVER (), 2)
        AS average_scenario_month_collections_rub
FROM months m
CROSS JOIN params cfg
CROSS JOIN current_metrics cm
LEFT JOIN forecast f ON f.month = m.month
ORDER BY m.month;


-- 6. Payer cohorts: compare observed cash LTV at equal ages (30/90/180 days).
-- Cohort is month of FIRST positive succeeded payment, not registration month.
-- LTV_N uses [first_payment_at, first_payment_at + N days).
-- Only users old enough to have a complete N-day observation enter that average.
-- Annual prepayments count in full when purchased, as in report 2.
-- These are revenue LTVs; margin/costs and future lifetime are not inferred.
WITH first_payments AS (
    SELECT p.user_id, MIN(p.created_at) AS first_paid_at
    FROM payments p
    WHERE p.status = 'succeeded' AND p.amount > 0
    GROUP BY p.user_id
), payer_ltv AS (
    SELECT
        fp.user_id,
        DATE_TRUNC('month', fp.first_paid_at AT TIME ZONE 'Europe/Moscow')::date
            AS cohort_month,
        COALESCE(SUM(p.base_amount::numeric), 0::numeric) AS observed_ltv_rub,
        COUNT(*) FILTER (WHERE p.base_amount IS NULL) AS unvalued_payments,
        CASE WHEN fp.first_paid_at + INTERVAL '30 days' <= CURRENT_TIMESTAMP THEN
            COALESCE(SUM(p.base_amount::numeric) FILTER (
                WHERE p.created_at < fp.first_paid_at + INTERVAL '30 days'
            ), 0::numeric)
        END AS ltv_30d_rub,
        CASE WHEN fp.first_paid_at + INTERVAL '90 days' <= CURRENT_TIMESTAMP THEN
            COALESCE(SUM(p.base_amount::numeric) FILTER (
                WHERE p.created_at < fp.first_paid_at + INTERVAL '90 days'
            ), 0::numeric)
        END AS ltv_90d_rub,
        CASE WHEN fp.first_paid_at + INTERVAL '180 days' <= CURRENT_TIMESTAMP THEN
            COALESCE(SUM(p.base_amount::numeric) FILTER (
                WHERE p.created_at < fp.first_paid_at + INTERVAL '180 days'
            ), 0::numeric)
        END AS ltv_180d_rub
    FROM first_payments fp
    JOIN payments p ON p.user_id = fp.user_id
        AND p.status = 'succeeded' AND p.created_at >= fp.first_paid_at
    GROUP BY fp.user_id, fp.first_paid_at
)
SELECT
    cohort_month,
    COUNT(*) AS payers,
    ROUND(AVG(observed_ltv_rub), 2) AS average_observed_ltv_rub,
    COUNT(ltv_30d_rub) AS mature_payers_30d,
    ROUND(AVG(ltv_30d_rub), 2) AS average_ltv_30d_rub,
    COUNT(ltv_90d_rub) AS mature_payers_90d,
    ROUND(AVG(ltv_90d_rub), 2) AS average_ltv_90d_rub,
    COUNT(ltv_180d_rub) AS mature_payers_180d,
    ROUND(AVG(ltv_180d_rub), 2) AS average_ltv_180d_rub,
    SUM(unvalued_payments) AS unvalued_payments
FROM payer_ltv
GROUP BY cohort_month
ORDER BY cohort_month;
