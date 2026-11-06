# Project Memory Index

Start with [AGENTS](../AGENTS.md) and the [documentation map](../docs/README.md).
These notes are context, not a second specification or live deployment record.

- [Topics/backups/nodes](project_features_added.md): operational feature context.
- [LTE guide](../docs/LTE_TRAFFIC.md): current accounting and conversion rules.

Code caveats observed on 2026-11-06; not fixed by documentation changes:

- `NameBonusService.sync_panel_for_user` derives panel status from expiry without
  checking the bot ban; a local fake-client check confirmed it can send ACTIVE
  for a banned user with a pending claim projection.
- `ProfileSyncMiddleware` can overwrite an LTE balance description on profile
  changes; the next LTE check writes the balance again.
- Old `lte:packages` callbacks check subscription history rather than uniformly
  requiring an active subscription. Do not assume a hidden button enforces this.

Recheck the affected source before changes. Keep secrets, customer data and
transient session logs out of memory; add notes only when useful for later work.
