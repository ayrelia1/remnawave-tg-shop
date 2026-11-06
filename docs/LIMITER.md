# Remnawave limiter webhook

The bot receives `POST /webhook/limiter` from remnawave-limiter. Contract checked
against [upstream c9211f5](https://github.com/syvlech/remnawave-limiter/tree/c9211f5266bf94ebd31bec6673e8e1ce465dabf0).
Only `violation_detected` events in `manual` mode are processed. Active IPs,
subnets or ASN groups are an estimate of device usage, not a physical device count.

## Configuration and rollout

Bot environment:

```dotenv
LIMITER_WEBHOOK_SECRET=<shared-secret>
SUPPORT_LINK=https://t.me/your_support
LIMITER_CHAT_THREAD_ID=15296
```

Limiter environment:

```dotenv
ACTION_MODE=manual
VIOLATION_THRESHOLD=1
COOLDOWN=300
WEBHOOK_URL=https://your-bot-domain/webhook/limiter
WEBHOOK_SECRET=<shared-secret>
WEBHOOK_SECRET_HEADER=false
```

Use the same private random secret in both processes. Without the bot secret,
the endpoint returns 503 and the worker is inactive. SUPPORT_LINK is required
when enabling the integration. The handler verifies HMAC-SHA256
`X-Signature-V2` over `<X-Timestamp>.<raw body>`, with five-minute clock tolerance.
It accepts JSON up to 256 KiB; the raw-secret header and V1 signature are not
authentication fallbacks. Synchronize the servers' clocks.

Deploy migration `0012_limiter_violations` before running the new bot/scheduler;
serialize migrations because both entry points normally apply them at startup.
Existing customers start with no warnings. Run one scheduler. Configure the
public HTTPS route and then enable limiter delivery. Check effective limiter
settings too: Redis runtime overrides can supersede environment values. Its
threshold must be 1 so every eligible detection reaches the bot; the bot owns
the three-warning threshold. Manual mode retains limiter's own admin alerts.

## Behavior

- `user.user_id` in the webhook is the stable numeric panel account ID. Resolve
  it through `Subscription.panel_user_uuid`, then find the Telegram owner.
  Count warnings per panel account, independently of other subscriptions of
  that owner. A rotating subscription URL/short UUID is not the counter key.
- First detection starts a fixed 48-hour window at durable bot receipt time.
  The second does not extend it. At the exact window end, a new detection starts
  at 1/3. Receipt time determines membership even if the worker runs after the
  window boundary; an unapplied event itself older than 48 hours is stale.
- Warnings 1/3 and 2/3 use the same key-revoke and HWID cleanup as manual reset.
  Send details and Connect (fresh link) / Support buttons with existing emoji.
  Expiry and LTE credit/usage remain unchanged.
- On 3/3, disable the panel account, set the matching local subscription's
  `status_from_panel=DISABLED`, `is_active=False`, and send Support only.
  `User.is_banned` is unchanged. Purchases/bonuses/operator actions may reactivate
  it under existing rules; there is no permanent limiter-ban flag. Another
  violation within the same window can disable it again at the capped 3/3.
- After persisting a valid event, the webhook immediately runs its reset/disable
  and notification steps. Processing failures return 503 so the limiter can
  retry the same event. A completed duplicate returns 200 without repeating the
  action. Requests can time out while panel/Telegram calls are still running;
  persisted checkpoints and user-row locks protect redelivery.
- An older unfinished action on the same account keeps priority; the newer
  request returns 503 rather than overtaking it. The scheduler is a recovery
  path for unfinished events, handling up to 100 eligible inbox entries per
  minute and respecting retry backoff. Successful webhook processing leaves
  nothing for it to repeat. It does not poll all panel users.
  Inactive/expired accounts, changed limits, conflicting owners and stale events
  do not receive a new penalty. No IP-disconnect command is sent.
- Every committed reset (1/3, 2/3) and disable (3/3) also logs to the existing
  `LOG_CHAT_ID`, in `LIMITER_CHAT_THREAD_ID` (fallback: `LOG_THREAD_ID`). The alert
  includes the action, counts, Telegram/panel IDs and the shared profile button.
  It uses NotificationService's ordinary best-effort admin queue, independently
  of customer-message retries. No extra delivery table or migration is needed.

## Storage, retries and rollback

`limiter_violations` is a durable inbox: local `subscription_id` (FK SET NULL),
stable `panel_user_ref`, Telegram owner snapshot, unique body hash, receipt/report
times, fixed-window bounds/number, counts/grouping, processing stage, reset-token
hash, retry metadata and completion time. It stores no raw IP list, webhook body
or subscription URL. Replacing a local subscription row preserves the panel's
warning window; a different panel ID starts a separate history.

Stages are pending → resetting/disabling → applied → done, or ignored. Checkpoint
before revoke, compare the hashed token after a lost response, and retry failed
HWID cleanup/notifications without blindly rotating again. Webhook and scheduler
use the same processor. Scheduled retries back off from one minute up to 64
minutes; a signed redelivery can resume its own event immediately. Blocking the bot ends notification attempts but
does not undo the account action. Failed notifications do not delay later
reset/disable actions; superseded notices are suppressed. A delayed third-warning
notice rechecks panel status and is suppressed if access has been reactivated.
Telegram delivery is not transactional with
SQL: a crash after send and before commit can repeat a message, not a strike.
Processed/ignored rows become eligible for pruning after 48 hours plus a
five-minute transaction grace. Window history needed by an unfinished event is
retained until that event is handled. User-row locks serialize bot operations;
they cannot make external panel administrator edits atomic with bot requests.

To pause/roll back, stop upstream delivery and disable the bot secret; preserve
the table and existing disabled statuses. Do not downgrade/drop the table if
pending events/history must survive. The integration does not automatically
reenable disabled subscriptions. Tests use SQLite/fake clients; real PostgreSQL
concurrency and deployed limiter/panel behavior need an approved integration check.
