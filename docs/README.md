# Documentation map

Keep documentation small: one maintained place for each topic. Read the guide
relevant to the task and verify details in source rather than duplicating code.

| Document | Purpose |
| --- | --- |
| [Root README](../README.md) | Features, installation and operator configuration |
| [AGENTS](../AGENTS.md) | Shared coding-agent entry: architecture, code map, contracts and checks |
| [DESIGN](../DESIGN.md) | Telegram messages, buttons, localization and premium emoji |
| [LTE traffic](LTE_TRAFFIC.md) | LTE balances, squads, notices, admin adjustments, backfill and controller logs |
| [Limiter webhook](LIMITER.md) | Signed violation events, subscription warnings, key resets and disabling |
| [SQL analytics](../scripts/analytics/README.md) | Report definitions, currency and calendar/cohort semantics |
| [Memory](../memory/MEMORY.md) | Brief implementation context and caveats to recheck |

For exact schema use [models](../db/models.py) and
[migrations](../alembic/versions/); for settings use
[Settings](../config/settings.py) and [.env.example](../.env.example).
No separate copies of every DB column or environment variable are maintained.

`docs` explains durable behavior; `memory` keeps short context/decisions. Shared
agent guidance belongs in AGENTS; CLAUDE points to it. Update the owning guide
when behavior changes. Do not record private environment values or assume old
notes describe production. Documentation is not proof that every path is correct.
