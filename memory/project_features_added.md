---
name: Features Added - Topics, Backups, Node Monitor
description: Major features added: Telegram topic routing, daily DB backups with 7z encryption, Remnawave node health monitoring, admin buttons for manual backup/node check
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

**Node monitoring service** (`bot/services/node_monitor_service.py`):
- Polls /nodes API endpoint every NODE_MONITOR_INTERVAL_MINUTES (default 5)
- Alerts on node going down or recovering via statuses topic
- Tracks state per node by uuid/id/name

**Admin panel buttons** (in System Functions section):
- "💾 Бэкап БД" → manual backup trigger
- "🔍 Проверить ноды" → manual node status check

**Why:** User requested these features for operational monitoring and DR readiness.
**How to apply:** When discussing backups or node monitoring, reference these services. The backup requires Docker image rebuild to install pg_dump and 7za.

Also note: BACKUP_PASSWORD in .env is set to "change_me_strong_password" - user should change it to a strong password before production use.
