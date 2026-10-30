-- PostgreSQL / DBeaver: one overall summary, no calendar-month breakdown.
-- Lifetime ARPU includes every registered user, including users who never paid.
-- Active monthly ARPPU: average of each active payer's lifetime subscription
-- revenue divided by that user's purchased months; all historical tariffs count.
-- This weights purchased months within a user, then users equally.
-- Month metrics assume time subscriptions, not traffic packages.
-- Monthly totals are historical normalized estimates, not future collection forecasts.
-- base_amount is RUB in this project. Missing valuations are counted and excluded.
-- Migration 0007 backfilled old FX payments 1:1; audit those before relying on RUB totals.
-- Active paid = is_active, unexpired, provider present, positive subscription duration.
-- Bonus/trial months are not purchased months. Tariff conversions and gifts mean
-- purchased months need not equal the actual number of service months delivered.
-- Revenue is observed succeeded payment value, before fees/costs and refunds
-- not represented by payment status. LTV is observed to date, not predicted.
WITH payments_per_user AS (
    SELECT
        user_id,
        SUM(base_amount::numeric) AS lifetime_revenue,
        COUNT(*) FILTER (WHERE amount > 0) AS paid_count,
        COUNT(*) FILTER (WHERE base_amount IS NULL) AS unvalued_count,
        SUM(base_amount::numeric) FILTER (
            WHERE amount > 0 AND subscription_duration_months > 0
        ) AS subscription_revenue,
        SUM(subscription_duration_months) FILTER (
            WHERE amount > 0
              AND subscription_duration_months > 0
              AND base_amount IS NOT NULL
        ) AS purchased_months
    FROM payments
    WHERE status = 'succeeded'
    GROUP BY user_id
), per_user AS (
    SELECT
        u.user_id,
        COALESCE(p.lifetime_revenue, 0) AS lifetime_revenue,
        COALESCE(p.paid_count, 0) AS paid_count,
        COALESCE(p.unvalued_count, 0) AS unvalued_count,
        p.subscription_revenue / NULLIF(p.purchased_months, 0) AS monthly_spend,
        EXISTS (
            SELECT 1
            FROM subscriptions s
            WHERE s.user_id = u.user_id
              AND s.is_active IS TRUE
              AND s.end_date > CURRENT_TIMESTAMP
              AND s.provider IS NOT NULL
              AND s.duration_months > 0
        ) AS is_active_payer
    FROM users u
    LEFT JOIN payments_per_user p ON p.user_id = u.user_id
)
SELECT
    ROUND(AVG(lifetime_revenue), 2)
        AS "За всё время на пользователя, ₽",
    ROUND(AVG(monthly_spend) FILTER (WHERE is_active_payer), 2)
        AS "В месяц на активного платящего, ₽",
    ROUND(COALESCE(SUM(monthly_spend) FILTER (WHERE is_active_payer), 0), 2)
        AS "В месяц со всех активных, оценка, ₽",
    ROUND(AVG(lifetime_revenue) FILTER (WHERE paid_count > 0), 2)
        AS "Накопленный LTV платящего, ₽",
    COUNT(*) FILTER (WHERE is_active_payer AND monthly_spend IS NOT NULL)
        AS "Активных в месячном расчёте",
    COALESCE(SUM(unvalued_count), 0)
        AS "Платежей без валютного пересчёта"
FROM per_user;
