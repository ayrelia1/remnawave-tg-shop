# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Telegram bot for selling VPN/proxy subscriptions via the [Remnawave](https://remnawave.com) panel. Users purchase subscriptions through the bot, which provisions access on the Remnawave panel via API. Built with Python, Aiogram 3.x, PostgreSQL, and aiohttp.

## Running the Project

```bash
# Copy and configure environment
cp .env.example .env
# Edit .env with credentials

# Start with Docker (recommended)
docker compose up -d
docker compose logs -f remnawave-tg-shop

# Database migrations run automatically on startup
# To run manually:
alembic upgrade head
```

The bot requires a public HTTPS URL for webhooks (`WEBHOOK_BASE_URL` in .env). The aiohttp server listens on `WEB_SERVER_HOST:WEB_SERVER_PORT` (default `0.0.0.0:8080`).

## Architecture

**Entry point**: `main.py` → `bot/main_bot.py::run_bot()`

**Startup sequence**: Load settings → connect PostgreSQL → run Alembic migrations → build services → start aiohttp webhook server → set Telegram webhook → register routers.

**Request flow**:
```
POST /webhook/telegram → Aiogram dispatcher → Middlewares → Router → Handler → Service → DAL → DB
```

**Webhook endpoints** (all handled by aiohttp in `bot/app/web/`):
- `/webhook/telegram` — Telegram updates
- `/webhook/yookassa`, `/webhook/freekassa`, `/webhook/platega`, `/webhook/severpay`, `/webhook/cryptopay` — payment provider callbacks
- `/webhook/panel` — Remnawave panel events

## Code Structure

| Path | Purpose |
|------|---------|
| `config/settings.py` | Pydantic BaseSettings — all config with computed properties |
| `bot/main_bot.py` | Startup/shutdown, webhook registration |
| `bot/routers.py` | Aggregates all routers; admin handlers behind `AdminFilter(ADMIN_IDS)` |
| `bot/handlers/user/` | User-facing: start, subscription views, payment flows, referral, trial, promo, partner cabinet |
| `bot/handlers/admin/` | Admin panel: stats, user management, broadcast, sync, logs, ad/partner campaigns |
| `bot/services/` | Business logic layer (subscription, panel API, payments, referral, promo, notifications) |
| `db/models.py` | SQLAlchemy ORM: User, Subscription, Payment, UserBilling, UserPaymentMethod, PromoCode, PromoCodeActivation, ActiveDiscount, MessageLog, PanelSyncStatus, AdCampaign, AdAttribution, CampaignAccrual, PartnerPayout, CurrencyRate |
| `db/dal/` | Data Access Layer — one module per entity |
| `bot/middlewares/` | DB session, i18n, ban check, channel gate, action logger, profile sync |
| `bot/app/` | Dispatcher factory, service factory, aiohttp web server |
| `locales/ru.json`, `locales/en.json` | i18n translation strings |
| `alembic/versions/` | Database migration files |

## Key Patterns

**Settings**: All config in `config/settings.py` as a single Pydantic model. Computed properties build `DATABASE_URL`, webhook URLs, bytes-from-GB traffic limits, and `ADMIN_IDS` list from comma-separated string.

**Services**: Instantiated once in `bot/app/factories/build_services.py`, injected into handlers via Aiogram middleware data dict. The largest service is `subscription_service.py` (~56KB) — handles subscription creation, renewal, expiry checks, device-tier switching, and panel sync.

**Database access**: Always use `AsyncSession` injected by `DBSessionMiddleware`. DAL classes wrap queries; services call DAL. Never write raw SQL outside of DAL modules.

**Payment integration**: Each provider has its own handler file (`payments_yookassa.py`, etc.) and service class. The `PAYMENT_METHODS_ORDER` setting controls UI ordering. Each provider requires both a creation flow (user initiates) and a webhook handler (provider confirms).

**i18n**: Call `i18n.gettext(lang, key, **kwargs)` where `lang` comes from middleware. Translation keys are in `locales/ru.json` and `locales/en.json`. The `DEFAULT_LANGUAGE` setting is the fallback.

**Premium emoji in copy**: locale strings may contain `::name::` tokens (e.g. `::bolt::`, `::lock::`, `::calendar::`). `JsonI18n._load_locales` expands them once at startup into `<tg-emoji emoji-id="…">glyph</tg-emoji>` using `TEXT_EMOJI` in `bot/constants/premium_emoji.py` — the same id registry that feeds `icon_custom_emoji_id` on inline buttons, so a pack swap is still a one-file change. Expansion happens *before* `str.format`, so tokens never collide with `{placeholders}`; unknown tokens are left in place and logged. `PREMIUM_EMOJI_ENABLED=false` degrades every token to its bare unicode glyph — set it when the bot has no Fragment username, since Telegram rejects custom emoji entities from such bots. **Tokens are only valid in strings sent with `parse_mode=HTML`.** Button labels (`*_button`) and `callback.answer(..., show_alert=True)` texts are plain text and must stay on bare unicode. Button labels additionally carry **no glyph at all** — the icon comes from `icon_custom_emoji_id` on the button itself, so an emoji in the label renders *next to* the premium icon instead of being it. `tests/test_partner_keyboards.py` enforces this over every `*_button` string.

**Admin access**: Handlers under `bot/handlers/admin/` are automatically gated by `AdminFilter` in `bot/routers.py`. Add new admin routers to the `admin_router_aggregate` there.

**Subscription tiers**: When `DEVICE_PLANS_ENABLED=true` the bot sells device-based tiers (e.g. 5/10/15 devices), each with its own per-duration price matrix (`DEVICE_PLANS_RUB` / `DEVICE_PLANS_STARS`, parsed in `config/settings.py`). The purchased tier is stored on `Subscription.hwid_device_limit` / `Payment.hwid_device_limit` and pushed to the panel as `hwidDeviceLimit`. Buying a *different* tier while a sub is active is a "tariff switch": remaining days are converted value-preservingly by price ratio (`tier_monthly_rate`) in `subscription_service.activate_subscription`, mirrored by `compute_switch_preview` for the pre-payment warning. Upgrade/downgrade switches are gated by `TARIFF_SWITCH_UPGRADE_ENABLED` / `TARIFF_SWITCH_DOWNGRADE_ENABLED` (`is_switch_allowed`). Trial/bonus/legacy subs have `hwid_device_limit=NULL` and stack time instead of switching. When device plans are off, `device_limit` stays `None` and the panel keeps its global limit.

**Money is normalised on the payment, once.** Every `payments` row carries `base_amount` and the `fx_rate` that produced it, frozen when the payment is created — `payment_dal.create_payment_record` is the single chokepoint every provider goes through, and the two success transitions fill it in later if the rate was missing at the time. Rates live in `currency_rates`, edited from the admin panel (Системные функции → Курсы валют); `PARTNER_CURRENCY_RATES` in .env is read *only* by migration 0007 to seed that table. **The base currency's own rate is an identity, not a setting** — everything else is measured in it, so `set_rate` refuses any value but 1.0 for it, `delete_rate` refuses it outright, the admin row is not an edit button, and `ensure_base_rate` forces it back on every visit to the screen. Editing it would have rescaled every future payment in the base currency while leaving history frozen, producing a report that silently mixes two scales. Because the value is stored, every money total — `get_financial_statistics`, `get_user_total_paid`, `get_referral_revenue`, campaign revenue — is a `SUM(base_amount)` and they cannot disagree with each other. Editing a rate never revalues anything: it applies to payments made afterwards, plus payments that were never valued at all.

A currency with no configured rate leaves `base_amount`/`fx_rate` NULL rather than defaulting to 1.0 — counting 5 USDT as 5 RUB is the exact bug this design removes. The sale still goes through; the payment simply stays out of every total and is surfaced by `count_unvalued_payments` (global) and `ad_dal.count_unpriced_payments` (per campaign). Adding the rate and calling `payment_dal.revalue_unvalued_payments` fills the gap, which the admin rates screen does automatically on save.

**Ad labels & partner programs**: `ad_campaigns` rows carry a `campaign_type` — `"ad"` (traffic we bought; `cost` is the spend) or `"partner"` (an affiliate label bound to `partner_user_id` earning `partner_percent` of the revenue it brings). A `/start <start_param>` writes one `ad_attributions` row per user, **first-touch and permanent**: `ensure_attribution` returns the existing row whatever campaign is passed, and `user_id` is the PK, so a user can never be re-attributed.

**A campaign is credited only for users it actually brought.** `ad_attributions.is_new_user` is True only when the labelled `/start` was also the user's registration — `start.py` derives it from whether the `users` row was created in that same request (`registered_now`). Someone who was already a user before clicking still gets an attribution row, flagged False, and is excluded from every campaign figure by `_campaign_users`: starts, trials, payers, revenue, and the accrual ledger alike. Without that flag an existing customer clicking a partner link would earn that partner a share of purchases they had nothing to do with. Global money (financial statistics, LTV, referral revenue) is untouched by the flag — the revenue is real, it simply is not the campaign's.

`campaign_accruals` is the campaign-side ledger: one immutable row per attributed payment, written when the payment reaches `succeeded` (hooked inside `payment_dal.mark_provider_payment_succeeded_once` and `update_payment_status_by_db_id`). It does **no currency math** — `base_amount` is copied from the payment. What it adds is the campaign side of the deal: the `percent` in force at that moment and the resulting `earned_amount`, so a later percent change cannot rewrite history either. `payment_id` is `UNIQUE`, which is what stops a replayed webhook double-crediting a partner, and `ON DELETE SET NULL`, so purging a customer keeps the money record. `sync_campaign_accruals` is the completeness net readers call first: it materialises rows for eligible payments that have none yet (idempotent, skips still-unvalued payments).

Payouts (`partner_payouts`, admin-recorded, cascade-deleted with the campaign) are normalised the same way at entry, so `balance = SUM(earned_amount) - SUM(payouts.base_amount)`. `PARTNER_MIN_PAYOUT` is display-only — withdrawals go through support. Partners open their cabinet with `/partner`; every callback re-checks `campaign.partner_user_id == from_user.id` via `_owned_campaign`. Deleting a partner user detaches and deactivates the label instead of removing it.

Out of the box only Stars are non-RUB: YooKassa/FreeKassa/Platega/SeverPay write `RUB`, Heleket hardcodes `RUB` (`HELEKET_TO_CURRENCY` only picks the coin Heleket settles in, it never reaches the ledger), and CryptoPay writes `CRYPTOPAY_ASSET`, which is `RUB` under the default `CRYPTOPAY_CURRENCY_TYPE=fiat`.


## Payment Providers

Six providers supported, each toggled by `{PROVIDER}_ENABLED=true` in .env:
- **YooKassa** — Russian cards, supports receipts and auto-renew
- **FreeKassa** — Russian aggregator
- **Platega** — Russian QR/cards/crypto
- **SeverPay** — Russian processor
- **CryptoPay** — Telegram crypto wallet
- **Telegram Stars** — Native Telegram currency (no external provider)

## Remnawave Panel Integration

The bot provisions VPN access by calling the Remnawave panel REST API (`PANEL_API_URL` + `PANEL_API_KEY`). Users are registered and subscriptions created/extended via `bot/services/panel_api_service.py`. The panel can also send webhook events back to `/webhook/panel`.

**Target API version: Remnawave 3.x (verified against 3.2.1).** Contract source of truth: `libs/contract/` in [remnawave/backend](https://github.com/remnawave/backend/tree/3.2.1/libs/contract). Key facts a change here must respect:

- **Users are identified by a numeric `id`; `User.uuid` no longer exists** (dropped in 3.0). `extract_panel_user_ref()` reads `id` and falls back to `uuid`, `panel_user_id()` coerces a stored reference to the int the API needs and returns `None` for legacy UUIDs. The DB column is still named `panel_user_uuid` and holds this reference as an opaque string.
- **Removed routes**: `/users/by-telegram-id`, `/users/by-email`, `/users/by-tag`, `/users/by-id`, `/system/tools/happ/encrypt` (gone since 2.8.0, so `CRYPT4_ENABLED` is dead on 2.8+). Lookups by telegramId/email now go through `GET /users/stream`, which takes them as first-class indexed query filters and paginates by cursor. `by-username` and `by-short-uuid` survive.
- `PATCH /api/users` identifies the user by `id` (or `username`) in the body — the identity is injected by `update_user_details_on_panel`, so `_build_panel_update_payload` must not add one.
- HWID bodies take `userId`, not `userUuid`; `POST /hwid/devices/delete-all` replaces the per-device loop.
- ~43 endpoints (every `DELETE`, async bulk ops, node restarts) answer **204/202 with an empty body**; `_request` normalises that into `{"status": "success", "response": None, "empty_body": True}`.
- **Stale references self-heal**: when the panel returns a reference different from the stored one, `_get_or_create_panel_user_link_details` (and the admin sync) rewrite `users.panel_user_uuid` *and* call `subscription_dal.rebind_panel_user_reference` so reference-filtered subscription lookups keep working. This is what carries a database over a 2.x → 3.x panel upgrade — there is no Alembic migration for it.
- **Webhook events**: 2.8.0 collapsed `user.expires_in_{72,48,24}_hours` / `user.expired_24_hours_ago` into a single `user.expiration` event with `meta.expiration` = signed hours (negative before expiry, positive after). `panel_webhook_service.py` maps both the new and legacy forms; only the ±24/48/72h buckets have message templates. Requires `EXPIRATION_NOTIFICATIONS_ENABLED=true` on the panel. `meta` is a top-level sibling of `data`, not nested inside it.
- **"User absent" is three error codes, not one**: `A025` USER_NOT_FOUND (by-id, DELETE, actions), `A062` USERS_NOT_FOUND (collections), `A063` GET_USER_BY_UNIQUE_FIELDS_NOT_FOUND — the one `by-username`/`by-short-uuid` actually returns. Use `PanelApiService._is_absent()` (all three, plus any 404) rather than an inline allowlist; missing `A063` turns a routine "not provisioned yet" into a logged hard error. Note 5xx never reaches these checks — `_raise_if_transient` converts it to `PanelUnavailableError` first.

## Testing

```bash
pip install -r requirements-dev.txt
pytest
```

`tests/` covers the panel API contract (routes and payloads), the HTTP envelope handling, webhook event mapping, and the panel-reference relinking. The panel tests are fully stubbed — no network — and `tests/conftest.py` provides `RecordingPanelApiService`, which records `(method, endpoint, kwargs)` per call and replays scripted responses.

The money tests are the exception: `test_partner_program.py` runs the DAL against in-memory SQLite (`aiosqlite`) with the real models, so the arithmetic is exercised as SQL — payment normalisation, windowed aggregates, rate/percent freezing, webhook replay, and the unvalued-currency path. `test_partner_keyboards.py` checks callback/handler coverage, the 64-byte callback limit, one-row-per-entry layout, that button labels carry no glyph, and that every `_( "key", ...)` call matches its locale template in both languages. `test_migrations.py` guards the single Alembic head, migration/model column parity across revisions, and the backfill invariants.

Postgres-specific behaviour is not covered by pytest — verify it against a throwaway `postgres:17-alpine` container when touching migrations. The check that matters: upgrade a populated database from 0004 to head and confirm every money figure (financial statistics, user LTV, referral revenue, per-campaign revenue) is byte-identical before and after, since existing payments are valued 1:1.

## Database Migrations

When adding new model fields or tables:
1. Modify `db/models.py`
2. Run `alembic revision --autogenerate -m "description"`
3. Review the generated file in `alembic/versions/`
4. Test with `alembic upgrade head`
