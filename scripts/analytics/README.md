# Revenue and customer analytics

Run `all_reports.sql` as a SQL script in DBeaver to obtain six result sets, or
select an entire individual query and execute it. Each individual file contains
one read-only statement and no blank lines inside the statement. Result column
names and overview metric labels are Russian. No views, temporary tables,
settings, production connections, or application changes are required.

## Reports

| File | Grouping | Main question |
| --- | --- | --- |
| `01_overview.sql` | Current overall snapshot | How much has a user paid, and what is an active payer's historical monthly spend? |
| `02_monthly_payments.sql` | Creation month of succeeded payments | How are collections, first purchases, returning buyers, and purchased terms changing? |
| `03_registration_cohorts.sql` | Registration month and observation window | How well do users acquired in a given month convert and monetize? |
| `04_first_payment_cohorts.sql` | First positive payment month, first purchased term, observation window | How do payer LTV and repeat purchases compare at equal ages and initial terms? |
| `05_tariff_mix.sql` | All time / payment month, recorded device limit and term | Which plans generate revenue and what is their actual monthly price? |
| `06_data_quality.sql` | Data-quality checks | Can these numbers be trusted and reconciled? |

Read report 6 first. Then inspect the overview and monthly results. Use the
registration report for acquisition/conversion, and the first-payment report for
customer value and repeat purchases. Do not add overlapping report totals.

## Time and money contracts

- Calendar grouping uses `Europe/Moscow`. A registration in September with the
  first purchase in October belongs to the September registration cohort and
  the October first-payment cohort; its payment is October collections.
- `payments.created_at` is creation time, not confirmed receipt time. There is no
  immutable actual-payment timestamp in the schema. `updated_at` is mutable and
  is intentionally not substituted. Month attribution follows the bot's reports.
- Only `status = 'succeeded'` contributes to money totals. A payer has at least
  one such payment with `amount > 0`. Buyer counts use distinct users, not orders.
- Money is summed from `base_amount::numeric`, currently RUB. This reflects
  recorded discounts and the frozen conversion rate, before costs/fees/tax and
  any refunds not represented by status. No net-profit metric is inferred.
- `base_amount IS NULL` is excluded from money totals and counted as missing.
  Never use `COALESCE(base_amount, amount)` for mixed currencies. Rates for old
  non-RUB payments need review: migration 0007 backfilled them at 1:1. A 1:1
  rate is a review flag, not proof that a particular payment is wrong.
- Successful records with future creation dates are excluded from time-based
  reports. Undated payments contribute to the overview and all-time tariff
  totals, but cannot be assigned to a payment month or first-payment cohort.
  Report 6 flags them, so totals can be reconciled explicitly.
- All reports operate on retained records. Deleting a user cascades to their
  payments in this model, so deleted historical business cannot be recovered.
- Across multiple queries `CURRENT_TIMESTAMP` can differ slightly if new
  payments arrive while the script runs. The queries do not alter transaction
  isolation or change the database to enforce a shared snapshot.

## Current overview definitions

An active paying user currently has an `is_active = true`, unexpired time
subscription with a recorded provider and a positive purchased duration, plus a
positive succeeded purchase in retained history. The test does not check
`start_date <= now`: prepaid extensions can move that mutable field forward.
Trials without provider/payment evidence are excluded. Banned/grace-period
accounts remain included when they satisfy this same local subscription
predicate, consistent with the prior report; banned active payers are counted.

- Lifetime ARPU: known succeeded revenue divided by **all registered users**,
  including users with no purchases.
- Observed payer LTV: mean known lifetime revenue among users who paid at least
  once. Inactive former payers are included. Active payer LTV is a separate row.
- Historical monthly spend of an active payer: for **each** current active payer,
  sum known positive time-purchase revenue and divide by the matching purchased
  months, then average those user rates equally. A purchase of RUB 200 for one
  month plus RUB 1,200 for twelve months gives RUB 1,400 / 13 = RUB 107.69/month.
  This is not the mean of transaction-level monthly prices, nor a calendar-time
  average since registration. Free days and gaps between purchases are excluded.
- The sum of those historical user rates is a normalized estimate, not guaranteed
  future collections and not exact current MRR. It intentionally preserves the
  user's original historical-rate definition instead of switching to list prices
  or the latest purchase. Missing valuations/durations reduce coverage.
- Currently active / ever paid is a **current share**, not monthly retention.
  Repeat buyers / ever paid is an observed repeat-purchase share, not churn.

## Monthly payment columns

`Впервые плативших` counts users whose first retained positive payment is in that
payment month. `Вернувшихся платящих` counts buyers whose first payment was in an
earlier month. The two counts partition that month's buyers; a second purchase
in the same first month does not reclassify a new buyer as returning.

`На платившего, ₽` is that month's known collections divided by that month's
positive buyers. It includes a full annual prepayment in its purchase month.
`На юзера базы, ₽` uses the number registered by period end (or now for the
current month), including non-buyers. Undated registrations cannot enter that
historical base and are flagged by report 6.

`Мес. цена покупок, ₽` first computes known time-purchase revenue / matching
purchased months **for each buyer within that month**, then averages buyers.
It is a price/mix measure for that month's purchases; it is not the historical
active subscriber ARPPU or historical recognized revenue.

`Рост выручки, %` compares two calendar months. It is NULL for the incomplete
current month and when the previous month's revenue is zero. Zero-sale months
are present. The history starts in the earliest retained registration/payment
month; the initial business month can itself cover less than a full month.

## Cohort windows and interpretation

Observation windows are 30, 90, 180, and 400 exact 24-hour days, using half-open
intervals `[origin, origin + window)`. A user enters a window metric only when
the **entire** window has elapsed. Otherwise its contribution is NULL, not zero.
`С полным окном` shows the denominator, which can be smaller than the full cohort.

- Registration cohorts include non-payers as zeros. `Конверсия за окно, %` is
  the share of mature users with a positive purchase within that window.
  `На юзера за окно, ₽` includes all mature users, including non-payers.
- Payer cohorts start at the first retained positive purchase. `LTV за окно, ₽`
  is mean known revenue within the window among mature payers. Annual prepayments
  count in full when purchased. `Повтор за окно, %` means at least two positive
  purchases within that window; it does not establish service continuity.
- Payer cohorts are split by the **first purchased term**. Compare 1-month plans
  with 1-month plans and 12-month plans with 12-month plans. The 400-day window
  provides a longer observation horizon for annual purchasers; it is not a
  business grace period, nor an assertion that everyone must renew by day 400.
- `Активны сегодня, %` / `Платившие активны, %` always describe **now**, even on
  an old cohort row. They are not retention at day 30/90/180/400.
- Current lifetime metrics for recent cohorts are naturally younger. Compare
  fixed-window LTV/conversion with the same window and sufficient mature users.
  A one-user cohort is not as reliable a comparison as a large cohort.
- Registration timestamps imported later than historical payments invalidate
  acquisition-age interpretations. Report 6 flags payment-before-registration
  cases. Do not silently treat imported dates as original acquisition dates.

## Limits that the SQL cannot repair

Subscription rows are upserted at renewal. There is no immutable ledger of
service intervals, tariff conversions, gifts, all cancellations, refunds, and
historical active states. Accordingly, these reports do **not** invent exact
historical MRR, monthly subscriber churn, service retention, or predicted full
LTV. Reconstructing intervals from payment terms alone would miss existing trial
days, referral/promo/name bonuses and tier conversions.

Time-price and purchased-month metrics assume time subscriptions. Traffic-mode
payments reuse `subscription_duration_months` for a package size and are not
reliably identifiable from this schema. If historical traffic packages exist,
identify them from an authoritative source before using those price metrics.

The tariff report uses the recorded purchase's device limit. A NULL stays
`Не записан`; current configuration is not used to rewrite unknown old tariffs.
Buyer counts overlap across plans/months and must not be summed as unique users.

## Verification

`verify_reports.py` checks read-only structure, one statement per file, absence
of blank-line statement splits, and PostgreSQL's 63-byte identifier limit.
It also executes the report calculations over synthetic fixtures using the
standard-library SQLite engine with explicit PostgreSQL-to-SQLite adapters for
date functions, intervals, series, ordered-set median, and lateral VALUES.
That validates the report arithmetic and boundaries; it is **not** a PostgreSQL
integration test. No real database connection or production execution is made.

## Definition references

- [Stripe billing analytics](https://docs.stripe.com/billing/subscriptions/analytics)
  distinguishes monthly normalized subscription revenue, subscriber counts and
  estimated lifetime value. These scripts label historical price proxies explicitly.
- [DBeaver SQL execution](https://dbeaver.com/docs/dbeaver/SQL-Execution/)
  documents current-statement versus selected-text/script execution and the blank
  line delimiter setting. Execute the complete query from WITH through its semicolon.
