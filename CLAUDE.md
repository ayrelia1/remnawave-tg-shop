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
| `db/models.py` | SQLAlchemy ORM: User, Subscription, Payment, PromoCode, MessageLog |
| `db/dal/` | Data Access Layer — one module per entity |
| `bot/middlewares/` | DB session, i18n, ban check, channel gate, action logger, profile sync |
| `bot/app/` | Dispatcher factory, service factory, aiohttp web server |
| `locales/ru.json`, `locales/en.json` | i18n translation strings |
| `alembic/versions/` | Database migration files |

## Key Patterns

**Settings**: All config in `config/settings.py` as a single Pydantic model. Computed properties build `DATABASE_URL`, webhook URLs, bytes-from-GB traffic limits, and `ADMIN_IDS` list from comma-separated string.

**Services**: Instantiated once in `bot/app/factories/build_services.py`, injected into handlers via Aiogram middleware data dict. The largest service is `subscription_service.py` (~47KB) — handles subscription creation, renewal, expiry checks, and panel sync.

**Database access**: Always use `AsyncSession` injected by `DBSessionMiddleware`. DAL classes wrap queries; services call DAL. Never write raw SQL outside of DAL modules.

**Payment integration**: Each provider has its own handler file (`payments_yookassa.py`, etc.) and service class. The `PAYMENT_METHODS_ORDER` setting controls UI ordering. Each provider requires both a creation flow (user initiates) and a webhook handler (provider confirms).

**i18n**: Call `i18n.gettext(lang, key, **kwargs)` where `lang` comes from middleware. Translation keys are in `locales/ru.json` and `locales/en.json`. The `DEFAULT_LANGUAGE` setting is the fallback.

**Admin access**: Handlers under `bot/handlers/admin/` are automatically gated by `AdminFilter` in `bot/routers.py`. Add new admin routers to the `admin_router_aggregate` there.

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

## Database Migrations

When adding new model fields or tables:
1. Modify `db/models.py`
2. Run `alembic revision --autogenerate -m "description"`
3. Review the generated file in `alembic/versions/`
4. Test with `alembic upgrade head`
