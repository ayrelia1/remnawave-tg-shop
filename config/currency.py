"""The single definition of the currency this bot keeps its books in.

Every amount the bot *reports* — financial statistics, user LTV, referral
revenue, campaign revenue, partner balances — is expressed in this currency.
Payments themselves may arrive in anything (`payments.currency`); they are
normalised into `payments.base_amount` at purchase time using the rate table,
and it is `base_amount` that all the totals sum.

This is deliberately a constant rather than a setting. The value is not
configuration: it is baked into every historical `base_amount` already written,
into the `RUB_PRICE_*` price list, and into the accounts the payment providers
settle to. Changing it is a fork-level decision that also needs a data
migration to re-express existing rows — an env var would make that look like a
one-line switch, which is exactly the trap this module exists to close.

Note what is *not* covered here: the currency each provider is billed in
(`currency_code = "RUB"` in the payment handlers). That is a fact about the
merchant account, not a reporting choice, and changing it means changing the
provider setup too — so those stay written out where they are used.
"""

BASE_CURRENCY: str = "RUB"
