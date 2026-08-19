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
| `bot/handlers/user/` | User-facing: start, subscription views, payment flows, referral, trial, promo |
| `bot/handlers/admin/` | Admin panel: stats, user management, broadcast, sync, logs |
| `bot/services/` | Business logic layer (subscription, panel API, payments, referral, promo, notifications) |
| `db/models.py` | SQLAlchemy ORM: User, Subscription, Payment, UserBilling, UserPaymentMethod, PromoCode, PromoCodeActivation, ActiveDiscount, MessageLog, PanelSyncStatus, AdCampaign, AdAttribution |
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

**Premium emoji in copy**: locale strings may contain `::name::` tokens (e.g. `::bolt::`, `::lock::`, `::calendar::`). `JsonI18n._load_locales` expands them once at startup into `<tg-emoji emoji-id="…">glyph</tg-emoji>` using `TEXT_EMOJI` in `bot/constants/premium_emoji.py` — the same id registry that feeds `icon_custom_emoji_id` on inline buttons, so a pack swap is still a one-file change. Expansion happens *before* `str.format`, so tokens never collide with `{placeholders}`; unknown tokens are left in place and logged. `PREMIUM_EMOJI_ENABLED=false` degrades every token to its bare unicode glyph — set it when the bot has no Fragment username, since Telegram rejects custom emoji entities from such bots. **Tokens are only valid in strings sent with `parse_mode=HTML`.** Button labels (`*_button`) and `callback.answer(..., show_alert=True)` texts are plain text and must stay on bare unicode.

**Admin access**: Handlers under `bot/handlers/admin/` are automatically gated by `AdminFilter` in `bot/routers.py`. Add new admin routers to the `admin_router_aggregate` there.

**Subscription tiers**: When `DEVICE_PLANS_ENABLED=true` the bot sells device-based tiers (e.g. 5/10/15 devices), each with its own per-duration price matrix (`DEVICE_PLANS_RUB` / `DEVICE_PLANS_STARS`, parsed in `config/settings.py`). The purchased tier is stored on `Subscription.hwid_device_limit` / `Payment.hwid_device_limit` and pushed to the panel as `hwidDeviceLimit`. Buying a *different* tier while a sub is active is a "tariff switch": remaining days are converted value-preservingly by price ratio (`tier_monthly_rate`) in `subscription_service.activate_subscription`, mirrored by `compute_switch_preview` for the pre-payment warning. Upgrade/downgrade switches are gated by `TARIFF_SWITCH_UPGRADE_ENABLED` / `TARIFF_SWITCH_DOWNGRADE_ENABLED` (`is_switch_allowed`). Trial/bonus/legacy subs have `hwid_device_limit=NULL` and stack time instead of switching. When device plans are off, `device_limit` stays `None` and the panel keeps its global limit.

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

`tests/` covers the panel API contract (routes and payloads), the HTTP envelope handling, webhook event mapping, and the panel-reference relinking. Everything is stubbed — no network, no database. `tests/conftest.py` provides `RecordingPanelApiService`, which records `(method, endpoint, kwargs)` per call and replays scripted responses.

## Database Migrations

When adding new model fields or tables:
1. Modify `db/models.py`
2. Run `alembic revision --autogenerate -m "description"`
3. Review the generated file in `alembic/versions/`
4. Test with `alembic upgrade head`
