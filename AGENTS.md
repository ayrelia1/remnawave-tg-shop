# Project guidance

Telegram shop for Remnawave subscriptions and cumulative LTE traffic. Python
3.12, aiogram 3, aiohttp, async SQLAlchemy/asyncpg, PostgreSQL, Alembic and
APScheduler. Keep the existing requirements/toolchain and architecture.

Communicate in Russian; keep code and technical documentation in English.
User/session instructions take precedence. Preserve unrelated work and obtain
approval for material changes. Production access, deployment and Git writes
require explicit authorization. Never print private `.env` or customer secrets.

## Where to start

- [README](README.md): operator setup and features.
- [Documentation map](docs/README.md): existing topic guides and source references.
- [DESIGN](DESIGN.md): Telegram UI, localization and premium emoji conventions.
- [LTE guide](docs/LTE_TRAFFIC.md): accounting, legacy conversion and rollout.
- [Limiter](docs/LIMITER.md): signed inbox, subscription-scoped warning window and reset/disable processing.
- [Analytics](scripts/analytics/README.md): SQL metric and time-window definitions.
- [Memory](memory/MEMORY.md): short notes to verify against code, not a second spec.

## Architecture and code map

`main.py` initializes settings/DB/migrations and starts `bot/main_bot.py`.
The bot handles Telegram and provider/panel webhooks through aiohttp; a public
`WEBHOOK_BASE_URL` is required. `scheduler.py` is a separate process for LTE,
backups, optional panel sync and name-bonus checks. Run one scheduler.

| Path | Responsibility |
| --- | --- |
| `config/settings.py`, `.env.example` | Environment configuration, validators and derived catalogs/URLs |
| `bot/app/` | Dispatcher/middleware setup, service construction, HTTP routes |
| `bot/routers.py`, `bot/handlers/` | User/admin/inline flows; admin routes use `AdminFilter` |
| `bot/services/` | Subscription, panel API, payments, referrals, promos, LTE and notifications |
| `db/models.py`, `db/dal/` | ORM schema and reusable database queries |
| `db/database_setup.py`, `db/alembic_runner.py`, `alembic/versions/` | DB connections and migration chain |
| `locales/`, `bot/keyboards/`, `bot/constants/premium_emoji.py` | RU/EN messages, keyboards and shared emoji registry |
| `backfill_lte.py` | Explicit preview/apply conversion of old customers' LTE balances |
| `tests/` | Panel contract, business, migration and UI regressions |

Request flow: webhook → dispatcher → middleware → handler → service → DAL/DB.
Services are injected through dispatcher data. FSM and general message queues
are in memory; do not assume restart durability or shared multi-worker state.

## Important contracts

- Use the injected `AsyncSession`; middleware commits success and rolls back
  exceptions, while providers/jobs also have explicit commits. Never share a
  session between concurrent tasks. LTE lock order is `User` → `LteAccount`.
- Prices/device tiers use `DEVICE_PLANS_RUB` / `DEVICE_PLANS_STARS`; included
  bytes use `DEVICE_PLANS_LTE_GB` for 1/3/6/12 months. Packages/trial are also
  environment-configured. No fallback price list, traffic-only sales mode or
  active database catalog editor. Historical catalog objects remain in migrations.
- Trial devices use the smallest configured tier, then `USER_HWID_DEVICE_LIMIT`.
  Trial rows record devices; do not assume NULL or a hardcoded device count.
  Preserve renewal and tariff-switch day conversion in `SubscriptionService`.
- Invoices freeze price, discount, months, devices, `product_type` and LTE bytes.
  Fulfill recorded terms. `lte_topup` grants no days/referral bonus. Trace both
  checkout and success callbacks, including duplicates and failed activation.
- RUB is `BASE_CURRENCY` in code. Reports use frozen `base_amount`/`fx_rate`;
  missing valuation is explicit, not rate 1. Campaign attribution is first-touch;
  accrued money/percentage snapshots must not be rewritten by later edits.
- Remnawave 3.x uses numeric user `id`; legacy `panel_user_uuid` stores it as
  text. Preserve identity resolution/rebinding, successful empty 202/204 bodies
  and missing-user versus `PanelUnavailableError` handling.
- LTE wallet belongs to the Telegram user, not a subscription row. Credit is
  nonexpiring; grants are source-unique. Configured/manual GB are whole; usage
  and prorated grants keep exact bytes. One GB is `2**30` bytes.
- Meter deltas from the last observation; a changed panel identity establishes
  a baseline, a lower same-user counter starts a new epoch without refund.
  Panel is globally unlimited/NO_RESET; operator sets foreign/LTE multipliers
  to 0/1. Remove/restore only LTE squads, preserving unrelated squads; restoration
  needs credit, active future access and no ban. No forced IP disconnection.
- LTE bulk-reads pages up to 1000, updates changed users with five workers,
  re-reads only actual squad changes and defers conflicting bot-side changes.
  Preserve pending notice flags, warning rearming above 5 GB and cycle metrics.
  Legacy day-based credit requires explicit backfill, never automatic startup.
- Use RU/EN locale keys. Rich emoji tokens are for HTML bodies; button/alert
  text stays plain, buttons use shared `icon_custom_emoji_id`. Follow DESIGN.
- Limiter warnings belong to the stable panel ID, not Telegram user or rotating
  link. On 3/3 only disable that subscription; do not set User.is_banned or add a
  permanent activation guard. Webhooks process immediately; scheduler retries
  unfinished events through the same processor. Shared key reset preserves expiry/LTE state.

Main DB groups: User/Subscription/Payment; LteAccount/LteGrant; saved billing
methods; promos/discounts; name-bonus claims; action logs/sync status; campaign
attribution/accruals/payouts and currency rates. Exact fields/constraints belong
in `db/models.py` and migration history, not a duplicate schema in this file.

## Verification

Use the existing virtual environment and focused tests first, then broaden
according to risk. On Windows:

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m compileall -q bot config db main.py scheduler.py backfill_lte.py
git diff --check
```

On Linux use `.venv/bin/python`. Tests use fakes/SQLite and do not prove live
vendor behavior or PostgreSQL locking. Applied migrations must not be rewritten;
schema changes require approval and populated-data/rollback verification.
Both bot and scheduler startup apply migrations and have external effects:
do not run them, backfill or restore as smoke tests. For documentation-only work,
check links and diff. Report checks actually run and remaining limitations.
