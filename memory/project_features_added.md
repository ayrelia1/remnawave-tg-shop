---
name: Features Added - Topics, Backups, Node Webhook Monitoring
description: Major features added: Telegram topic routing, daily DB backups with 7z encryption, node status monitoring via Remnawave panel webhooks, admin button for manual backup
type: project
---

## Features added (2026-04-02)

**Topic-based notification routing** - All notifications now route to separate Telegram supergroup topics:
- Users (registrations, trials) → LOG_THREAD_ID_USERS
- Purchases (payments, promos) → LOG_THREAD_ID_PURCHASES
- Statuses (panel sync, node alerts, suspicious activity) → LOG_THREAD_ID_STATUSES
- Backups → LOG_THREAD_ID_BACKUPS

**Database backup service** (`bot/services/backup_service.py`):
- Daily scheduled pg_dump at BACKUP_HOUR (default 9am)
- Compressed with 7za (-t7z AES-256 encryption, password from BACKUP_PASSWORD)
- Sent as document to Telegram backup topic
- Requires: postgresql-client + p7zip-full in Dockerfile (added)

**Node monitoring via panel webhooks** (`bot/services/panel_webhook_service.py`):
- Node status alerts come from Remnawave panel webhooks (events: `node.offline`, `node.online`)
- `handle_node_event()` extracts name/address from payload, calls `notification_service.notify_node_down/recovered()`
- `notification_service.notify_node_down/recovered()` still exist and send to statuses topic
- Old polling-based `node_monitor_service.py` was deleted on 2026-04-16

**Admin panel buttons** (in System Functions section):
- "💾 Бэкап БД" → manual backup trigger

**Why:** User requested these features for operational monitoring and DR readiness. Node polling replaced by webhook events per user request on 2026-04-16.
**How to apply:** Node status notifications now only fire when Remnawave panel sends a webhook. No polling, no scheduler job, no NODE_MONITOR_ENABLED setting. The backup requires Docker image rebuild to install pg_dump and 7za.

Also note: BACKUP_PASSWORD in .env is set to "change_me_strong_password" - user should change it to a strong password before production use.
