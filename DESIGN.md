# Telegram interface

The product is an existing aiogram Telegram bot, with Russian and English
translations in `locales`. Preserve its existing message, inline keyboard,
premium emoji and Back/Cancel conventions. Telegram owns typography, colors,
focus, scrolling and layout; this project does not implement a web theme.

`bot/constants/premium_emoji.py` owns all custom emoji IDs. Message bodies use
the existing `::name::` locale tokens through `JsonI18n`, with ordinary glyphs
when premium text emoji are disabled. Inline buttons use `icon_custom_emoji_id`
and plain localized labels; callback alerts also stay plain text. Separate
message/header keys from labels or alerts when the same wording needs rich HTML.
Subscription prices, device tiers, traffic packages and trial settings belong
to the existing environment configuration. There is no administrator catalog
editor. Administrative LTE balance handlers use the existing administrator filter.
Subscriptions use only device-tier price matrices. Included LTE GB are configured
by duration in `DEVICE_PLANS_LTE_GB`, independently of device count and currency.
There is no alternate subscription price list or global duration switch.
Configured and manually entered LTE grants use whole GB. Fractional values are
rejected with localized admin feedback or a startup configuration error. Saved
invoice terms, consumption, remaining balances and prorated migration grants
keep exact bytes and can still contain fractional GB.

Subscriptions remain the primary product. LTE usage is explained in My
subscription and in the depletion notification, without advertising a paid
LTE tier. The common action label is "Докупить LTE трафик" / "Buy more LTE traffic".
My subscription offers traffic top-ups only with a current active subscription
or trial confirmed by the panel; a missing local subscription row does not hide
valid panel access. Without active panel access, it offers a subscription purchase instead. Package prices
reuse tariff price formatting: whole amounts have no trailing decimal zero.
The home screen shows the greeting, subscription validity and device count.
LTE balance details belong in My subscription.

Environment changes affect future invoices after a process restart. Issued
invoices retain their recorded price, period, devices and LTE allowance. Subscription
time and LTE credit are independent: unused credit survives expiration, while
network access requires an active subscription. Existing customers are exempt
from LTE enforcement until a separate migration.

User cards expose one LTE traffic entry point. Balance refresh, positive amount
entry, confirmation and return to the balance screen reuse the admin message
and inline-keyboard conventions. Invalid input retains its form; a stale or
cancelled confirmation cannot execute an adjustment. Saved balances are marked
when live panel usage is unavailable. Manual deductions cannot exceed the fresh
remaining balance; adjustments do not alter subscription time.

The 5 GB warning and depletion notice use the same top-up button and LTE-only
explanation. Warning delivery is once per threshold crossing, rearmed only
after the balance rises above 5 GB. The polling interval is at least one minute.

Admin statistics show LTE package sales separately: successful purchase counts,
sold GB and base-currency revenue for today, the week, the month and all time.
The block uses the same UTC cutoffs and frozen payment amounts as total revenue,
where these sales are already included. Subscription grants, trials and manual
credits are excluded. Sales without an exchange rate retain their counts and GB;
a warning explains that their revenue awaits valuation. New rows use the shared
premium emoji tokens and Russian/English locales.
