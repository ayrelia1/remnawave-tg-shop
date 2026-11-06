# Environment pricing and cumulative LTE traffic

Subscription prices and device tiers use `.env` variables `DEVICE_PLANS_RUB`
and `DEVICE_PLANS_STARS`. Prices map to 1/3/6/12 months; a zero or empty price
disables that duration for the tier and currency. No alternate single-tier
price list or global period switches are used. LTE top-ups use `TRAFFIC_PACKAGES` and
`STARS_TRAFFIC_PACKAGES` in the existing `<GB>:<price>` format. Both products
are available together. There is no database catalog or catalog editor in the
admin interface. Restart all bot workers and the scheduler after configuration
changes; settings are loaded at process startup.

`DEVICE_PLANS_LTE_GB=50,150,300,600` sets the exact LTE grants for 1/3/6/12
months, independent of device count and payment currency. Four whole,
non-negative GB values are required; zero grants no LTE credit. Invalid values
prevent startup. The grant is made once on payment, not in monthly installments.
A top-up grants its
configured package size without extending subscription time. Displayed GB use
2^30 bytes, matching the existing bot convention.

Trial configuration uses `TRIAL_ENABLED`, `TRIAL_DURATION_DAYS` and
`TRIAL_TRAFFIC_LIMIT_GB` (default 10 GB of LTE). Its device limit follows the
previous rule: the smallest active device tier, otherwise
`USER_HWID_DEVICE_LIMIT`. Check existing deployment files: explicitly set
`TRIAL_TRAFFIC_LIMIT_GB=10` if the former global trial limit was `0` (unlimited).
It now controls the LTE grant only; `0` grants no LTE credit. Foreign servers
remain unlimited. These settings affect new trials, not existing ones.

Configured package sizes, plan/trial allowances and manual adjustments must use
whole GB. Fractional configured values prevent startup instead of producing an
unusable checkout option. Usage, remaining balances and prorated legacy grants
continue to use exact bytes and may display fractional GB. Previously issued
invoices retain their recorded byte amounts, including fractional-GB packages.

The home screen shows subscription validity and devices without an LTE balance
line. Remaining LTE traffic and top-up access are shown in My subscription.

New payments freeze `product_type` (`subscription` or `lte_topup`), original
price, duration, device count and LTE bytes. Fulfillment uses that snapshot,
including invoices issued before the return to environment pricing. Later
environment changes do not change an issued invoice. Existing payment amounts,
provider identities and subscription dates are not rewritten.

Migration history through `0011_lte_admin_alerts` is preserved for compatibility
with already migrated installations. Its historical `subscription_plans`,
`traffic_packages`, `trial_settings` tables and payment catalog-reference columns
may remain in the database, but the application no longer maps, seeds or reads
them. Existing catalog edits are not exported into `.env` automatically; verify
the environment values before updating. No new migration or database deletion
is required for this change.

LTE credit belongs to the user, independently of subscription rows. Unused
credit survives expiration, renewal and device-tier changes. A top-up adds
credit without extending subscription time or enabling an expired account.
Bonus, promotional and referral days do not add LTE credit. Grants have unique
sources so repeated payment processing cannot duplicate traffic credit.

## Preparing enforcement

This change does not configure a running Remnawave installation. Before enabling
the controller, the operator must:

1. Configure user consumption multipliers in the panel: foreign nodes `0`, LTE
   nodes `1`. Do not confuse these with node infrastructure billing multipliers.
2. Use dedicated LTE nodes/inbounds and separate LTE Internal Squads. Another
   squad must not grant the same LTE inbounds.
3. Set `LTE_SQUAD_UUIDS` to those squad UUIDs. Keep the ordinary squads in
   `USER_SQUAD_UUIDS`. The bot preserves unrelated current memberships.
4. Update all bot workers and the scheduler together. Keep the existing
   database backup. Startup applies migrations through `0011_lte_admin_alerts`.
   Use a maintenance window for schema changes and do not run
   old payment workers concurrently with the new product types.
5. Verify environment prices, trial settings and provider permissions. LTE
   control requires user reads and updates in the panel API.
6. Set `LTE_ENFORCEMENT_ENABLED=True` and run the existing standalone scheduler.
   `LTE_CHECK_INTERVAL_SECONDS` defaults to 60. Previous values below 60 seconds
   are normalized to 60; 120 seconds is also supported.

Each polling cycle reads `/users/stream` with the maximum supported page size
of 1000: a panel with 10,000 users needs approximately 10 read requests, rather
than 10,000 individual lookups. The snapshot exists only for that cycle. An
incomplete or malformed stream is discarded without changing balances/access.
Database transactions are opened only for changed accounts; unchanged counters,
membership, descriptions and notification flags require no per-user writes.
Up to five changed accounts are processed concurrently, within the existing DB
pool. Panel PATCH requests contain only changed fields. Concurrent purchases,
admin adjustments, panel relinking and subscription extensions are checked
under the existing user/account locks; affected users are deferred until the
next bulk cycle instead of applying an older snapshot as a counter reset. Notifications
are checked only for pending rows that were successfully verified this cycle.
Failed updates/deliveries are retried on the next cycle.
Only an actual LTE squad-membership change performs an additional individual
panel GET under the user/account locks, then recalculates usage, eligibility
and memberships using that fresh response. This preserves recent panel-admin
changes to unrelated squads and avoids restoring LTE to a recently disabled
or expired account. A read failure rolls back that user's changes for retry.
Ordinary accounting, unchanged users and description-only updates do not need
individual GETs. Panel read/modify/write is not atomic; a simultaneous manual
edit between the fresh GET and PATCH can still conflict. Interactive purchases
and admin balance operations retain their existing fresh individual reads.

Cycle completion is logged with user/change/pending-notice counts and duration.
The interval schedules a cycle; it is not a guarantee that a large cycle finishes
within that time. Existing scheduler overlap/coalescing rules still apply.

Global panel account limits remain `trafficLimitBytes=0`, with `NO_RESET`.
The panel collects traffic; the bot compares its cumulative lifetime counter
with user credit. The initial observation is a baseline, excluding earlier
traffic. When credit is exhausted, only the LTE squads are removed. Renewals
and top-ups recheck access.
The regular expiry, disabled status and user ban are respected by the scheduler.

The bot does not send connection-drop or IP-disconnect commands. Existing
connections may continue after squad removal; the panel and nodes apply their
own membership-change behavior. This does not change user-initiated HWID device
removal. Node capabilities and network setup remain operator-managed.

Subscription activations and bonus-day extensions restore ordinary squads from
`USER_SQUAD_UUIDS` even when LTE enforcement is paused or the user is exempt.
Existing unrelated memberships are preserved; unmanaged LTE memberships are
left as they are. Only enabled, managed LTE accounts have LTE membership
recomputed from their balance.

Bonus-day extensions preserve a local user's ban by keeping the panel account
`DISABLED`; extending its expiry does not restore network access. Unbanned users
can still regain access when a bonus extends an expired subscription.

Enforcement is asynchronous: panel collection, scheduler intervals, queues and
outages can allow additional usage. Actual observed usage remains charged;
credit is not reset or silently refunded after a counter reset. The bot never
resets panel traffic counters or changes node multipliers. Notifications are
queued in the database and retried; a crash after Telegram accepts a message
but before the database commit can cause the notification to be repeated.

Active users receive a low-balance warning once when their remaining balance
is positive and at most 5 GiB. Renewals, top-ups and manual credits rearm the
warning only when the balance rises above 5 GiB, so repeated small top-ups
below the threshold do not spam. At zero only the separate depletion notice
is queued. Both notices include the traffic top-up button. Durable pending/
notified flags survive restarts; unsuccessful Telegram sends remain pending.

## Panel user descriptions

With LTE enforcement enabled, the controller writes the managed user's remaining
LTE balance to the panel's `description`, for example `1 ГБ`, `0.5 ГБ`, or `0 ГБ`.
The number is rounded to two decimal places with trailing zeros removed; billing
continues to use exact bytes. The existing LTE checks, plan/trial activation,
top-ups and manual balance adjustments update the description. The controller
only includes it in a panel update when the formatted value differs, combining
it with a squad update when necessary. Failed synchronization is retried by the
existing scheduler.

Creation, Telegram ID repair and bulk panel synchronization no longer populate
descriptions with Telegram profile fields. Legacy-exempt (`managed=False`)
accounts keep their existing descriptions until transferred. After the one-time
transfer, the next enabled LTE check writes the transferred remaining balance.
Pausing LTE enforcement also pauses description updates. No new database
migration is required, and no subscription headers or host names are changed
automatically. Rolling back code does not restore former Telegram descriptions.

## Manual balance administration

Open **Admin → User card → LTE traffic** to refresh the current balance and
add/deduct a positive whole-GB amount. The screen shows net credit, usage and the
remaining balance. It explicitly labels saved values if the panel cannot be
read or enforcement is paused. Legacy-exempt accounts remain exempt after a
manual adjustment; only the one-time transfer enables them.

Adjustments require a confirmation and re-read panel usage under the same user
lock as purchases and the scheduler. A manual debit larger than the fresh
remaining balance is rejected; actual usage is never reduced to manufacture
credit. Signed `lte_grants.traffic_bytes` entries record corrections using an
`admin:<administrator_id>:<operation_token>` source. Paid grants remain positive;
negative entries require an admin source and no payment reference. Net credit
in `lte_accounts.credited_bytes` includes these signed corrections.

The balance commits before projecting squad access. Failed projection leaves
the adjustment recorded and access pending for the scheduler to retry, rather
than charging the same operation twice. No adjustment changes subscription time.
Migration `0011_lte_admin_alerts` can be downgraded before any negative ledger
entry exists; afterwards its downgrade deliberately refuses to discard debit
history. Roll back application code while preserving the signed ledger instead.

## Existing customers and rollback

Migration creates exempt LTE accounts (`managed=False`) for all existing bot
users. These legacy users are not metered and their LTE squads are not removed
until transferred. New accounts, including later imports, default to managed.
New purchases still record and accumulate their included credit. Transfer
existing users explicitly with `backfill_lte.py` after applying the database
migrations; application startup does not run the transfer.

### One-time transfer

Use the project's Python environment and the root `.env`. Back up the database,
pause purchases/subscription edits and keep the LTE scheduler stopped during
the transfer. Configure node consumption multipliers before taking baselines.

```sh
python backfill_lte.py --report lte-preview.csv
python backfill_lte.py --apply --report lte-applied.csv
```

The default is a read-only preview. `--apply` commits one user at a time and
creates a unique `legacy_lte_v1:<user_id>` ledger entry. Existing report files
are never overwritten. The report includes exact allowance bytes and panel
baselines, with `preview`, `applied`, `already_managed`, `needs_review` or
`error` outcomes. A nonzero exit code means errors/review items remain.

The command reads the panel once using cursor pages of 1000 users and reuses
that snapshot for all baselines. No individual panel lookups or panel writes
are made. Bot user IDs are also read in batches of 1000, with at most five
independent user transactions running at once. Each completed transaction is
reported immediately; CSV rows may be out of user-ID order. If the stream
cannot be read completely, no transfer is applied. Purchases/subscription edits
and LTE metering must remain paused throughout the transfer, as above.

The allowance for an active subscription is
`max(10 GiB, 75 GiB * remaining_seconds / (30 * 86400))`, rounded down to
whole bytes once. Partial days count; duration/device count do not multiply
the allowance. The latest active, unexpired subscription in the bot database
supplies the remaining time. Expired/unsubscribed users receive zero. Users
without a panel account are also marked managed, with no baseline until their
first panel account is provisioned.

The minimum applies to the one-time grant, in addition to existing credit.
Already transferred users are still skipped; rerunning the script neither
grants the minimum again nor revises a transfer recorded before this change.

For a panel-linked user the command must read a valid lifetime traffic counter
before committing anything. It preserves existing purchased top-ups and manual
credit, records the current panel counter as the baseline and sets
`managed=True`. Old panel usage is not charged and no panel counters or squad
memberships are changed. Accounts with existing new-plan/trial grants or
previously recorded usage are marked `needs_review` rather than double-granted.
Synchronize mismatched panel/subscription references before retrying.

The command skips already managed users with recorded grants/usage. Empty newly
imported accounts using the managed default still receive their one-time grant.
If interrupted, committed users remain
recorded and a rerun continues without duplicating grants or resetting their
baselines. Inspect all errors/review rows, then start the scheduler with LTE
enforcement enabled. It applies squad membership using the migrated balances.
Before enforcement starts, a mistaken transfer can be recovered from the
database backup. After metering or subsequent purchases, retain the ledger and
use explicit corrections instead of restoring old balances over new activity.

To pause enforcement, disable `LTE_ENFORCEMENT_ENABLED`; balances and the ledger
remain intact. This pauses synchronization and does not automatically restore
previously removed LTE squads. Re-enabling resumes charging from the recorded
counter, so prepare node multipliers before the first enablement and do not
change them during a pause without a deliberate accounting migration.

Do not drop the new tables to roll back application code after traffic has
been sold. First stop new top-up sales and settle outstanding invoices using
the new code. Preserve credits and payment snapshots for forward
recovery. Production migrations, deployment and customer backfill are separate
operator actions.
