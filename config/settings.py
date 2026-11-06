import logging
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, HttpUrl, ValidationError, computed_field, field_validator, model_validator
from typing import Optional, List, Dict, Any
from .traffic import GIB, traffic_bytes

# Canonical month durations for device-based subscription tiers.
# The comma-separated price lists in DEVICE_PLANS_* map positionally to these.
DEVICE_PLAN_MONTHS: List[int] = [1, 3, 6, 12]


class Settings(BaseSettings):
    BOT_TOKEN: str
    ADMIN_IDS_STR: str = Field(
        default="",
        alias="ADMIN_IDS",
        description="Comma-separated list of admin Telegram User IDs")

    POSTGRES_USER: str = Field(default="user")
    POSTGRES_PASSWORD: str = Field(default="password")
    POSTGRES_HOST: str = Field(default="localhost")
    POSTGRES_PORT: int = Field(default=5432)
    POSTGRES_DB: str = Field(default="vpn_shop_db")

    DEFAULT_LANGUAGE: str = Field(default="ru")
    PREMIUM_EMOJI_ENABLED: bool = Field(
        default=True,
        description=(
            "Render ::token:: placeholders in locale strings as Telegram custom "
            "(premium) emoji. Requires a bot with a Fragment username; set to "
            "false to fall back to plain unicode glyphs."
        ),
    )

    BROADCAST_EXPIRED_MIN_DAYS: int = Field(
        default=5,
        ge=1,
        description=(
            "Win-back broadcast target: only reach users whose subscription "
            "ended at least this many days ago. Users with a running "
            "subscription, or one that lapsed more recently, are skipped."
        ),
    )

    SUPPORT_LINK: Optional[str] = Field(default=None)
    SERVER_STATUS_URL: Optional[str] = Field(default=None)
    TERMS_OF_SERVICE_URL: Optional[str] = Field(default=None)
    PRIVACY_POLICY_URL: Optional[str] = Field(default=None)
    REQUIRED_CHANNEL_SUBSCRIBE_TO_USE: bool = Field(
        default=False,
        description="Require users to subscribe to REQUIRED_CHANNEL_ID before using the bot",
    )
    REQUIRED_CHANNEL_ID: Optional[int] = Field(
        default=None,
        description="Telegram channel ID the user must join to access the bot")
    REQUIRED_CHANNEL_LINK: Optional[str] = Field(
        default=None,
        description="Public username or invite link to the required channel for join button")

    YOOKASSA_SHOP_ID: Optional[str] = None
    YOOKASSA_SECRET_KEY: Optional[str] = None
    YOOKASSA_RETURN_URL: Optional[str] = None

    YOOKASSA_DEFAULT_RECEIPT_EMAIL: Optional[str] = Field(default=None)
    YOOKASSA_VAT_CODE: int = Field(default=1)
    YOOKASSA_TAX_SYSTEM_CODE: Optional[int] = Field(
        default=None,
        description="Tax system code for YooKassa receipts (1..6 per 54-FZ)"
    )
    YOOKASSA_PAYMENT_MODE: Optional[str] = Field(
        default=None,
        description="Optional override for YooKassa receipt item payment_mode."
    )
    YOOKASSA_PAYMENT_SUBJECT: Optional[str] = Field(
        default=None,
        description="Optional override for YooKassa receipt item payment_subject."
    )
    # Single toggle to enable recurring payments (saving cards, managing payment methods, auto-renew)
    YOOKASSA_AUTOPAYMENTS_ENABLED: bool = Field(default=False)
    YOOKASSA_AUTOPAYMENTS_REQUIRE_CARD_BINDING: bool = Field(
        default=True,
        description="When true, new YooKassa payments in autopay mode force card binding without a user checkbox."
    )

    LKNPD_INN: Optional[str] = Field(
        default=None,
        alias="NALOGO_INN",
        description="INN for lknpd.nalog.ru (self-employed) authentication"
    )
    LKNPD_PASSWORD: Optional[str] = Field(
        default=None,
        alias="NALOGO_PASSWORD",
        description="Password for lknpd.nalog.ru (self-employed) authentication"
    )
    LKNPD_API_URL: str = Field(
        default="https://lknpd.nalog.ru/api",
        alias="NALOGO_API_URL",
        description="Base URL for LKNPD API (can be overridden for proxies)"
    )
    LKNPD_RECEIPT_NAME_SUBSCRIPTION: str = Field(
        default="subscription {months} months",
        alias="NALOGO_RECEIPT_NAME_SUBSCRIPTION",
        description="Receipt item name for time-based subscriptions. Use {months} placeholder for duration."
    )
    LKNPD_RECEIPT_NAME_TRAFFIC: str = Field(
        default="traffic package {gb} GB",
        alias="NALOGO_RECEIPT_NAME_TRAFFIC",
        description="Receipt item name for traffic packages. Use {gb} placeholder for traffic amount."
    )

    WEBHOOK_BASE_URL: Optional[str] = None
    TELEGRAM_WEBHOOK_PATH: str = Field(
        default="/webhook/telegram",
        description="Relative path for Telegram webhook endpoint",
    )
    TELEGRAM_WEBHOOK_SECRET: Optional[str] = Field(
        default=None,
        description="Secret token for Telegram webhook header validation",
    )

    CRYPTOPAY_TOKEN: Optional[str] = None
    CRYPTOPAY_NETWORK: str = Field(default="mainnet")
    CRYPTOPAY_CURRENCY_TYPE: str = Field(default="fiat")
    CRYPTOPAY_ASSET: str = Field(default="RUB")
    CRYPTOPAY_ENABLED: bool = Field(default=True)
    PLATEGA_ENABLED: bool = Field(default=False)
    PLATEGA_BASE_URL: str = Field(default="https://app.platega.io")
    PLATEGA_MERCHANT_ID: Optional[str] = None
    PLATEGA_SECRET: Optional[str] = None
    PLATEGA_PAYMENT_METHOD: int = Field(
        default=2, description="Platega payment method ID (e.g., 2 for SBP QR)"
    )
    PLATEGA_RETURN_URL: Optional[str] = Field(default=None)
    PLATEGA_FAILED_URL: Optional[str] = Field(default=None)
    PLATEGA_CLIENT_COMMISSION_PERCENT: float = Field(
        default=0.0,
        description=(
            "DEPRECATED, no longer used. The webhook now accepts any overpayment, so the "
            "client-side commission no longer has to be kept in sync with the Platega "
            "cabinet. Kept so existing .env files keep parsing."
        ),
    )

    CISPAY_ENABLED: bool = Field(default=False)
    CISPAY_BASE_URL: str = Field(default="https://api.cispay.app")
    CISPAY_SHOP_ID: Optional[str] = None
    CISPAY_API_KEY: Optional[str] = None
    CISPAY_RETURN_URL: Optional[str] = Field(default=None)
    CISPAY_FAILED_URL: Optional[str] = Field(default=None)

    PAY2328_ENABLED: bool = Field(default=False)
    PAY2328_PROJECT_ID: Optional[str] = Field(
        default=None, description="2328 project UUID from the project settings"
    )
    PAY2328_API_KEY: Optional[str] = Field(
        default=None,
        description="2328 payment API key (not the Payout API key).",
    )
    PAY2328_BASE_URL: str = Field(default="https://api.2328.io/api")
    PAY2328_TO_CURRENCY: Optional[str] = Field(
        default=None,
        description=(
            "Optional preselected cryptocurrency (e.g. USDT); requires PAY2328_NETWORK. "
            "Leave both empty to let the payer choose on the hosted checkout."
        ),
    )
    PAY2328_NETWORK: Optional[str] = Field(default=None)
    PAY2328_TTL_SECONDS: int = Field(
        default=3600,
        ge=300,
        le=86400,
        description="Invoice validity window; 2328 allows 300..86400 seconds.",
    )
    PAY2328_RETURN_URL: Optional[str] = Field(default=None)
    PAY2328_SUCCESS_URL: Optional[str] = Field(default=None)

    @field_validator('PAY2328_TO_CURRENCY', 'PAY2328_NETWORK', mode='before')
    @classmethod
    def normalize_pay2328_direction(cls, value):
        if isinstance(value, str):
            return value.strip().upper() or None
        return value

    @model_validator(mode='after')
    def validate_pay2328_direction(self):
        if bool(self.PAY2328_TO_CURRENCY) != bool(self.PAY2328_NETWORK):
            raise ValueError("PAY2328_TO_CURRENCY and PAY2328_NETWORK must be set together")
        return self

    FREEKASSA_ENABLED: bool = Field(default=False)
    FREEKASSA_MERCHANT_ID: Optional[str] = None
    FREEKASSA_FIRST_SECRET: Optional[str] = None
    FREEKASSA_SECOND_SECRET: Optional[str] = None
    FREEKASSA_PAYMENT_URL: str = Field(default="https://pay.freekassa.ru/")
    FREEKASSA_API_KEY: Optional[str] = None
    FREEKASSA_PAYMENT_IP: Optional[str] = None
    FREEKASSA_PAYMENT_METHOD_ID: Optional[int] = None

    SEVERPAY_ENABLED: bool = Field(default=False)
    SEVERPAY_MID: Optional[int] = None
    SEVERPAY_TOKEN: Optional[str] = None
    SEVERPAY_RETURN_URL: Optional[str] = None
    SEVERPAY_BASE_URL: str = Field(default="https://severpay.io/api/merchant")
    SEVERPAY_LIFETIME_MINUTES: Optional[int] = Field(
        default=None,
        description="Lifetime of the payment link in minutes (30-4320, defaults to provider value)",
    )

    YOOKASSA_ENABLED: bool = Field(default=True)
    STARS_ENABLED: bool = Field(default=True)
    STARS_PROVIDER_TOKEN: Optional[str] = Field(
        default="",
        description="Provider token for Telegram invoices. For Stars (XTR) should stay empty.",
    )
    PAYMENT_METHODS_ORDER: Optional[str] = Field(
        default=None,
        description="Comma-separated list of payment methods to show (e.g., platega,cispay,yookassa,stars,2328,cryptopay)",
    )

    # Device-based subscription tiers, with prices for each duration:
    #   "<devices>:<1m>,<3m>,<6m>,<12m>|<devices>:..."
    # Zero or empty prices disable a duration for that tier and currency.
    DEVICE_PLANS_RUB: Optional[str] = Field(default=None)
    DEVICE_PLANS_STARS: Optional[str] = Field(default=None)
    DEVICE_PLANS_LTE_GB: str = Field(
        default="50,150,300,600",
        description="Whole LTE GB granted for 1/3/6/12 months, independent of device count",
    )

    @field_validator("DEVICE_PLANS_LTE_GB")
    @classmethod
    def validate_device_plan_lte(cls, value: str) -> str:
        parts = value.split(",")
        if len(parts) != len(DEVICE_PLAN_MONTHS):
            raise ValueError("DEVICE_PLANS_LTE_GB requires four GB amounts for 1/3/6/12 months")
        for part in parts:
            traffic_bytes(part.strip(), allow_zero=True)
        return value

    @computed_field
    @property
    def device_plan_lte_bytes(self) -> Dict[int, int]:
        return {months: traffic_bytes(gb.strip(), allow_zero=True)
                for months, gb in zip(DEVICE_PLAN_MONTHS, self.DEVICE_PLANS_LTE_GB.split(","))}

    # Allow/forbid switching to a more expensive / cheaper tier while a
    # subscription is active. Mirrors how Remnawave shop bots gate tariff changes.
    TARIFF_SWITCH_UPGRADE_ENABLED: bool = Field(default=True)
    TARIFF_SWITCH_DOWNGRADE_ENABLED: bool = Field(default=True)

    PANEL_WEBHOOK_SECRET: Optional[str] = Field(default=None)

    TRAFFIC_PACKAGES: Optional[str] = Field(
        default=None,
        description="Comma-separated list of traffic packages in the format '<GB>:<price>', e.g. '10:199,50:799'",
    )
    STARS_TRAFFIC_PACKAGES: Optional[str] = Field(
        default=None,
        description="Comma-separated list of traffic packages priced in Stars, e.g. '5:500,20:1500'",
    )

    @field_validator("TRAFFIC_PACKAGES", "STARS_TRAFFIC_PACKAGES")
    @classmethod
    def validate_package_gb(cls, value: Optional[str]) -> Optional[str]:
        for entry in (value or "").split(","):
            if ":" in entry:
                traffic_bytes(entry.split(":", 1)[0].strip())
        return value

    SUBSCRIPTION_NOTIFICATIONS_ENABLED: bool = Field(default=True)
    SUBSCRIPTION_NOTIFY_ON_EXPIRE: bool = Field(default=True)
    SUBSCRIPTION_NOTIFY_AFTER_EXPIRE: bool = Field(default=True)
    SUBSCRIPTION_NOTIFY_DAYS_BEFORE: int = Field(default=3)

    REFERRAL_BONUS_DAYS_INVITER_1_MONTH: Optional[int] = Field(
        default=3, alias="REFERRAL_BONUS_DAYS_1_MONTH")
    REFERRAL_BONUS_DAYS_INVITER_3_MONTHS: Optional[int] = Field(
        default=7, alias="REFERRAL_BONUS_DAYS_3_MONTHS")
    REFERRAL_BONUS_DAYS_INVITER_6_MONTHS: Optional[int] = Field(
        default=15, alias="REFERRAL_BONUS_DAYS_6_MONTHS")
    REFERRAL_BONUS_DAYS_INVITER_12_MONTHS: Optional[int] = Field(
        default=30, alias="REFERRAL_BONUS_DAYS_12_MONTHS")

    REFERRAL_BONUS_DAYS_REFEREE_1_MONTH: Optional[int] = Field(
        default=1, alias="REFEREE_BONUS_DAYS_1_MONTH")
    REFERRAL_BONUS_DAYS_REFEREE_3_MONTHS: Optional[int] = Field(
        default=3, alias="REFEREE_BONUS_DAYS_3_MONTHS")
    REFERRAL_BONUS_DAYS_REFEREE_6_MONTHS: Optional[int] = Field(
        default=7, alias="REFEREE_BONUS_DAYS_6_MONTHS")
    REFERRAL_BONUS_DAYS_REFEREE_12_MONTHS: Optional[int] = Field(
        default=15, alias="REFEREE_BONUS_DAYS_12_MONTHS")

    # Referral program configuration
    REFERRAL_ONE_BONUS_PER_REFEREE: bool = Field(
        default=True,
        description="When true, referral bonuses (for inviter and referee) are applied only once per invited user - on their first successful payment."
    )
    REFERRAL_ENABLED: bool = Field(
        default=True,
        description="Enable referral links, referral menu and referral bonuses",
    )
    LEGACY_REFS: bool = Field(
        default=True,
        description="Allow legacy referral links like ref_<telegram_id> to continue working. Defaults to True when unset."
    )

    PARTNER_PROGRAM_ENABLED: bool = Field(
        default=True,
        description="Enable partner (affiliate) campaigns and the /partner command",
    )
    PARTNER_MIN_PAYOUT: float = Field(
        default=500.0,
        description="Minimum partner balance (in the base currency) that can be withdrawn",
    )

    PANEL_API_URL: Optional[str] = None
    PANEL_API_KEY: Optional[str] = None
    SUBSCRIPTION_DOMAIN: Optional[str] = Field(
        default=None,
        description="Optional HTTP(S) origin for subscription links; preserves the panel-issued path and key",
    )
    USER_TRAFFIC_LIMIT_GB: Optional[float] = Field(default=0.0)
    USER_TRAFFIC_STRATEGY: str = Field(default="NO_RESET")
    LTE_ENFORCEMENT_ENABLED: bool = Field(default=False)
    LTE_SQUAD_UUIDS: Optional[str] = Field(default=None)
    LTE_CHECK_INTERVAL_SECONDS: int = Field(default=60, ge=10, le=3600)

    @field_validator("LTE_CHECK_INTERVAL_SECONDS")
    @classmethod
    def minimum_lte_check_interval(cls, value: int) -> int:
        # Preserve compatibility with previous .env files containing 30.
        return max(60, value)

    @model_validator(mode="after")
    def validate_lte_configuration(self):
        if self.LTE_ENFORCEMENT_ENABLED and not self.parsed_lte_squad_uuids:
            raise ValueError("LTE_SQUAD_UUIDS is required when LTE enforcement is enabled")
        return self
    USER_SQUAD_UUIDS: Optional[str] = Field(
        default=None,
        description=
        "Comma-separated UUIDs of internal squads to assign to new panel users")
    USER_EXTERNAL_SQUAD_UUID: Optional[str] = Field(
        default=None,
        description=
        "UUID of the external squad to assign to new panel users (optional)")

    TRIAL_ENABLED: bool = Field(default=True)
    TRIAL_DURATION_DAYS: int = Field(default=3)
    TRIAL_TRAFFIC_LIMIT_GB: Optional[int] = Field(default=10)

    @field_validator("TRIAL_TRAFFIC_LIMIT_GB", mode="before")
    @classmethod
    def validate_trial_gb(cls, value):
        return traffic_bytes(value, allow_zero=True) // GIB if value is not None else None

    NAME_BONUS_ENABLED: bool = Field(
        default=False,
        description="Enable the seven-day Telegram first-name bonus after any successful purchase",
    )

    CRYPT4_ENABLED: bool = Field(default=False, description="Enable happ crypt4 encryption for subscription URLs")
    CRYPT4_REDIRECT_URL: Optional[str] = Field(default=None, description="Base redirect URL used for the connect button when crypt4 is enabled")

    WEB_SERVER_HOST: str = Field(default="0.0.0.0")
    WEB_SERVER_PORT: int = Field(default=8080)
    LOGS_PAGE_SIZE: int = Field(default=10)

    SUBSCRIPTION_MINI_APP_URL: Optional[str] = Field(default=None)

    START_COMMAND_DESCRIPTION: Optional[str] = Field(default=None)
    PROMO_COMMAND_DESCRIPTION: Optional[str] = Field(default=None)
    PARTNER_COMMAND_DESCRIPTION: Optional[str] = Field(
        default=None,
        description=(
            "Set to publish /partner in the Telegram command menu for everyone. "
            "Leave unset to keep it a hidden command only partners are told about."
        ),
    )
    DISABLE_WELCOME_MESSAGE: bool = Field(default=False, description="Disable welcome message on /start command")

    MY_DEVICES_SECTION_ENABLED: bool = Field(
        default=False,
        description="Enable the My Devices section in the subscription menu"
    )
    USER_HWID_DEVICE_LIMIT: Optional[int] = Field(
        default=None,
        description="Default hardware device limit for panel users (0 = unlimited)"
    )
    
    # Inline mode thumbnail URLs
    INLINE_REFERRAL_THUMBNAIL_URL: str = Field(default="https://cdn-icons-png.flaticon.com/512/1077/1077114.png")
    INLINE_USER_STATS_THUMBNAIL_URL: str = Field(default="https://cdn-icons-png.flaticon.com/512/681/681494.png")
    INLINE_FINANCIAL_STATS_THUMBNAIL_URL: str = Field(default="https://cdn-icons-png.flaticon.com/512/2769/2769339.png")
    INLINE_SYSTEM_STATS_THUMBNAIL_URL: str = Field(default="https://cdn-icons-png.flaticon.com/512/2920/2920277.png")

    @computed_field
    @property
    def DATABASE_URL(self) -> str:
        return f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"

    @computed_field
    @property
    def ADMIN_IDS(self) -> List[int]:
        if self.ADMIN_IDS_STR:
            try:
                return [
                    int(admin_id.strip())
                    for admin_id in self.ADMIN_IDS_STR.split(',')
                    if admin_id.strip().isdigit()
                ]
            except ValueError:
                logging.error(
                    f"Invalid ADMIN_IDS_STR format: '{self.ADMIN_IDS_STR}'. Expected comma-separated integers."
                )
                return []
        return []

    @computed_field
    @property
    def PRIMARY_ADMIN_ID(self) -> Optional[int]:
        ids = self.ADMIN_IDS
        return ids[0] if ids else None

    @computed_field
    @property
    def trial_traffic_limit_bytes(self) -> int:
        if self.TRIAL_TRAFFIC_LIMIT_GB is None or self.TRIAL_TRAFFIC_LIMIT_GB <= 0:
            return 0
        return self.TRIAL_TRAFFIC_LIMIT_GB * GIB

    @computed_field
    @property
    def user_traffic_limit_bytes(self) -> int:
        # Per-account limits would also disconnect the unlimited foreign nodes.
        return 0

    @property
    def parsed_lte_squad_uuids(self) -> List[str]:
        from uuid import UUID
        return list(dict.fromkeys(str(UUID(value.strip()))
                                 for value in (self.LTE_SQUAD_UUIDS or "").split(",")
                                 if value.strip()))

    @computed_field
    @property
    def parsed_user_squad_uuids(self) -> Optional[List[str]]:
        if self.USER_SQUAD_UUIDS:
            return [
                uuid.strip()
                for uuid in self.USER_SQUAD_UUIDS.split(',')
                if uuid.strip()
            ]
        return None

    @computed_field
    @property
    def parsed_user_external_squad_uuid(self) -> Optional[str]:
        if self.USER_EXTERNAL_SQUAD_UUID:
            cleaned = self.USER_EXTERNAL_SQUAD_UUID.strip()
            if cleaned:
                return cleaned
        return None

    @computed_field
    @property
    def telegram_webhook_path(self) -> str:
        path = (self.TELEGRAM_WEBHOOK_PATH or "").strip() or "/webhook/telegram"
        if not path.startswith("/"):
            path = f"/{path}"
        return path

    @computed_field
    @property
    def telegram_full_webhook_url(self) -> Optional[str]:
        base = self.WEBHOOK_BASE_URL
        if base:
            return f"{base.rstrip('/')}{self.telegram_webhook_path}"
        return None

    @computed_field
    @property
    def yookassa_webhook_path(self) -> str:

        return "/webhook/yookassa"

    @computed_field
    @property
    def yookassa_full_webhook_url(self) -> Optional[str]:
        base = self.WEBHOOK_BASE_URL
        if base:
            return f"{base.rstrip('/')}{self.yookassa_webhook_path}"
        return None

    @computed_field
    @property
    def panel_webhook_path(self) -> str:
        return "/webhook/panel"

    @computed_field
    @property
    def panel_full_webhook_url(self) -> Optional[str]:
        base = self.WEBHOOK_BASE_URL
        if base:
            return f"{base.rstrip('/')}{self.panel_webhook_path}"
        return None

    @computed_field
    @property
    def cryptopay_webhook_path(self) -> str:
        return "/webhook/cryptopay"

    @computed_field
    @property
    def cryptopay_full_webhook_url(self) -> Optional[str]:
        base = self.WEBHOOK_BASE_URL
        if base:
            return f"{base.rstrip('/')}{self.cryptopay_webhook_path}"
        return None

    @computed_field
    @property
    def freekassa_webhook_path(self) -> str:
        return "/webhook/freekassa"

    @computed_field
    @property
    def freekassa_full_webhook_url(self) -> Optional[str]:
        base = self.WEBHOOK_BASE_URL
        if base:
            return f"{base.rstrip('/')}{self.freekassa_webhook_path}"
        return None

    @computed_field
    @property
    def severpay_webhook_path(self) -> str:
        return "/webhook/severpay"

    @computed_field
    @property
    def severpay_full_webhook_url(self) -> Optional[str]:
        base = self.WEBHOOK_BASE_URL
        if base:
            return f"{base.rstrip('/')}{self.severpay_webhook_path}"
        return None

    @computed_field
    @property
    def pay2328_webhook_path(self) -> str:
        return "/webhook/2328"

    @computed_field
    @property
    def pay2328_full_webhook_url(self) -> Optional[str]:
        base = self.WEBHOOK_BASE_URL
        if base:
            return f"{base.rstrip('/')}{self.pay2328_webhook_path}"
        return None

    @computed_field
    @property
    def platega_webhook_path(self) -> str:
        return "/webhook/platega"

    @computed_field
    @property
    def platega_full_webhook_url(self) -> Optional[str]:
        base = self.WEBHOOK_BASE_URL
        if base:
            return f"{base.rstrip('/')}{self.platega_webhook_path}"
        return None

    @computed_field
    @property
    def cispay_webhook_path(self) -> str:
        return "/webhook/cispay"

    @computed_field
    @property
    def cispay_full_webhook_url(self) -> Optional[str]:
        base = self.WEBHOOK_BASE_URL
        if base:
            return f"{base.rstrip('/')}{self.cispay_webhook_path}"
        return None

    # Effective YooKassa receipt fields.
    # Explicit .env values win; otherwise keep sensible defaults derived from the recurring toggle.
    @computed_field
    @property
    def yk_receipt_payment_mode(self) -> str:
        if self.YOOKASSA_PAYMENT_MODE:
            return self.YOOKASSA_PAYMENT_MODE
        return "full_payment" if self.YOOKASSA_AUTOPAYMENTS_ENABLED else "full_prepayment"

    @computed_field
    @property
    def yk_receipt_payment_subject(self) -> str:
        if self.YOOKASSA_PAYMENT_SUBJECT:
            return self.YOOKASSA_PAYMENT_SUBJECT
        return "service" if self.YOOKASSA_AUTOPAYMENTS_ENABLED else "payment"

    @staticmethod
    def _parse_device_plans(raw: Optional[str], as_int: bool) -> Dict[int, Dict[int, float]]:
        """Parse "<devices>:<1m>,<3m>,<6m>,<12m>|..." into {devices: {months: price}}."""
        plans: Dict[int, Dict[int, float]] = {}
        cleaned = (raw or "").strip()
        if not cleaned:
            return plans
        for tier_chunk in cleaned.split("|"):
            chunk = tier_chunk.strip()
            if not chunk or ":" not in chunk:
                continue
            devices_str, prices_str = chunk.split(":", 1)
            try:
                devices = int(devices_str.strip())
            except ValueError:
                logging.warning("Invalid DEVICE_PLANS tier devices skipped: %s", chunk)
                continue
            if devices <= 0:
                continue
            prices = [p.strip() for p in prices_str.split(",")]
            tier_map: Dict[int, float] = {}
            for idx, months in enumerate(DEVICE_PLAN_MONTHS):
                if idx >= len(prices):
                    break
                value = prices[idx]
                if not value:
                    continue
                try:
                    price_val = int(float(value)) if as_int else float(value)
                except ValueError:
                    logging.warning("Invalid DEVICE_PLANS price '%s' in %s", value, chunk)
                    continue
                if price_val > 0:
                    tier_map[months] = price_val
            if tier_map:
                plans[devices] = tier_map
        return plans

    @computed_field
    @property
    def device_plans_active(self) -> bool:
        """Whether at least one subscription offer is available."""
        return bool(self.device_subscription_options or self.device_stars_options)

    @computed_field
    @property
    def device_subscription_options(self) -> Dict[int, Dict[int, float]]:
        """{devices: {months: rub_price}} for configured positive prices."""
        return self._parse_device_plans(self.DEVICE_PLANS_RUB, as_int=False)

    @computed_field
    @property
    def device_stars_options(self) -> Dict[int, Dict[int, int]]:
        """{devices: {months: stars_price}}, available only with Stars enabled."""
        if not self.STARS_ENABLED:
            return {}
        return {devices: {months: int(price) for months, price in prices.items()}
                for devices, prices in self._parse_device_plans(self.DEVICE_PLANS_STARS, as_int=True).items()}

    @computed_field
    @property
    def device_tiers(self) -> List[int]:
        """Sorted list of available device counts (e.g. [5, 10, 15])."""
        return sorted(set(self.device_subscription_options) | set(self.device_stars_options))

    @computed_field
    @property
    def base_device_limit(self) -> Optional[int]:
        """Smallest configured device tier; used as default for trial/bonus subs."""
        tiers = self.device_tiers
        return tiers[0] if tiers else None

    def get_plan_price(self, devices: int, months: int) -> Optional[float]:
        return self.device_subscription_options.get(devices, {}).get(months)

    def get_plan_stars_price(self, devices: int, months: int) -> Optional[int]:
        return self.device_stars_options.get(devices, {}).get(months)

    def tier_monthly_rate(self, devices: int) -> Optional[float]:
        """Canonical price-per-month for a tier, taken from its longest period.

        Used for value-preserving day conversion when switching tiers; the
        constant factor cancels in old/new ratios, so only relativity matters.
        """
        tier_map = self.device_subscription_options.get(devices)
        if not tier_map:
            return None
        longest = max(tier_map.keys())
        price = tier_map.get(longest)
        if not price or longest <= 0:
            return None
        return float(price) / float(longest)

    @computed_field
    @property
    def traffic_packages(self) -> Dict[int, float]:
        """
        Mapping of traffic size in GB to price in the default currency.
        """
        packages: Dict[int, float] = {}
        raw = (self.TRAFFIC_PACKAGES or "").strip()
        if not raw:
            return packages
        for part in raw.split(","):
            chunk = part.strip()
            if not chunk or ":" not in chunk:
                continue
            size_str, price_str = chunk.split(":", 1)
            try:
                size_gb = traffic_bytes(size_str.strip()) // GIB
                price_val = float(price_str.strip())
                if size_gb > 0 and price_val >= 0:
                    packages[size_gb] = price_val
            except ValueError:
                logging.warning("Invalid TRAFFIC_PACKAGES entry skipped: %s", chunk)
                continue
        return packages

    @computed_field
    @property
    def stars_traffic_packages(self) -> Dict[int, int]:
        """
        Mapping of traffic size in GB to price in Telegram Stars.
        """
        packages: Dict[int, int] = {}
        raw = (self.STARS_TRAFFIC_PACKAGES or "").strip()
        if not raw:
            return packages
        for part in raw.split(","):
            chunk = part.strip()
            if not chunk or ":" not in chunk:
                continue
            size_str, price_str = chunk.split(":", 1)
            try:
                size_gb = traffic_bytes(size_str.strip()) // GIB
                price_val = int(float(price_str.strip()))
                if size_gb > 0 and price_val >= 0:
                    packages[size_gb] = price_val
            except ValueError:
                logging.warning("Invalid STARS_TRAFFIC_PACKAGES entry skipped: %s", chunk)
                continue
        return packages


    @computed_field
    @property
    def referral_bonus_inviter(self) -> Dict[int, int]:
        bonuses: Dict[int, int] = {}
        if self.REFERRAL_BONUS_DAYS_INVITER_1_MONTH is not None:
            bonuses[1] = self.REFERRAL_BONUS_DAYS_INVITER_1_MONTH
        if self.REFERRAL_BONUS_DAYS_INVITER_3_MONTHS is not None:
            bonuses[3] = self.REFERRAL_BONUS_DAYS_INVITER_3_MONTHS
        if self.REFERRAL_BONUS_DAYS_INVITER_6_MONTHS is not None:
            bonuses[6] = self.REFERRAL_BONUS_DAYS_INVITER_6_MONTHS
        if self.REFERRAL_BONUS_DAYS_INVITER_12_MONTHS is not None:
            bonuses[12] = self.REFERRAL_BONUS_DAYS_INVITER_12_MONTHS
        return bonuses

    @computed_field
    @property
    def referral_bonus_referee(self) -> Dict[int, int]:
        bonuses: Dict[int, int] = {}
        if self.REFERRAL_BONUS_DAYS_REFEREE_1_MONTH is not None:
            bonuses[1] = self.REFERRAL_BONUS_DAYS_REFEREE_1_MONTH
        if self.REFERRAL_BONUS_DAYS_REFEREE_3_MONTHS is not None:
            bonuses[3] = self.REFERRAL_BONUS_DAYS_REFEREE_3_MONTHS
        if self.REFERRAL_BONUS_DAYS_REFEREE_6_MONTHS is not None:
            bonuses[6] = self.REFERRAL_BONUS_DAYS_REFEREE_6_MONTHS
        if self.REFERRAL_BONUS_DAYS_REFEREE_12_MONTHS is not None:
            bonuses[12] = self.REFERRAL_BONUS_DAYS_REFEREE_12_MONTHS
        return bonuses

    @computed_field
    @property
    def yookassa_autopayments_active(self) -> bool:
        """Autopay features are available only when YooKassa itself is enabled."""
        return bool(self.YOOKASSA_ENABLED and self.YOOKASSA_AUTOPAYMENTS_ENABLED)

    @computed_field
    @property
    def payment_methods_order(self) -> List[str]:
        """
        Ordered list of payment providers to show in the subscription payment keyboard.
        """
        default_order = [
            "platega",
            "cispay",
            "freekassa",
            "severpay",
            "yookassa",
            "stars",
            "2328",
            "cryptopay",
        ]
        if not self.PAYMENT_METHODS_ORDER:
            return default_order
        methods = []
        for item in self.PAYMENT_METHODS_ORDER.split(","):
            slug = item.strip().lower()
            if slug:
                methods.append(slug)
        return methods or default_order
    
    # Logging Configuration
    LOG_LEVEL: str = Field(
        default="INFO",
        description="Global log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
    )
    LOG_CHAT_ID: Optional[int] = Field(default=None, description="Telegram chat/group ID for sending notifications")
    LOG_THREAD_ID: Optional[int] = Field(default=None, description="Thread ID for supergroup messages (optional)")
    LOG_STORE_MESSAGE_CONTENT: bool = Field(
        default=False,
        description="Store message/callback content in message logs",
    )
    LOG_STORE_RAW_UPDATES: bool = Field(
        default=False,
        description="Store raw update previews in message logs",
    )
    LOG_EXPORT_INCLUDE_SENSITIVE: bool = Field(
        default=False,
        description="Include content/raw update fields in admin CSV export",
    )
    
    LOG_ADMIN_HIDE: bool = Field(
        default=False,
        description="Hide admin-generated events from admin logs UI and CSV export",
    )

    @field_validator('SUBSCRIPTION_DOMAIN', mode='before')
    @classmethod
    def normalize_subscription_domain(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None
        domain = HttpUrl(value)
        if (
            domain.username is not None
            or domain.password is not None
            or domain.path not in (None, "", "/")
            or domain.query is not None
            or domain.fragment is not None
        ):
            raise ValueError(
                "SUBSCRIPTION_DOMAIN must contain only an HTTP(S) scheme and host, with an optional port"
            )
        return str(domain).rstrip("/")

    @field_validator('LOG_LEVEL', mode='before')
    @classmethod
    def normalize_log_level(cls, v):
        if isinstance(v, str):
            v = v.strip().upper()
        if not v:
            return "INFO"
        return v

    @model_validator(mode='before')
    @classmethod
    def drop_comment_placeholder_values(cls, values: Any):
        """
        dotenv parses lines like `KEY=  # comment` as `"# comment"`.
        Treat such values as unset so defaults/optionals work as expected.
        """
        if not isinstance(values, dict):
            return values

        sanitized: Dict[str, Any] = {}
        for key, value in values.items():
            if isinstance(value, str):
                trimmed = value.strip()
                if trimmed == "#" or trimmed.startswith("# "):
                    continue
            sanitized[key] = value
        return sanitized

    @field_validator(
        'TELEGRAM_WEBHOOK_PATH',
        mode='before',
    )
    @classmethod
    def normalize_webhook_path(cls, v):
        if not isinstance(v, str):
            return "/webhook/telegram"
        cleaned = v.strip()
        if not cleaned:
            return "/webhook/telegram"
        if not cleaned.startswith("/"):
            cleaned = f"/{cleaned}"
        return cleaned

    @field_validator(
        'REQUIRED_CHANNEL_LINK',
        'PAY2328_RETURN_URL',
        'PAY2328_SUCCESS_URL',
        'PLATEGA_RETURN_URL',
        'PLATEGA_FAILED_URL',
        'CISPAY_RETURN_URL',
        'CISPAY_FAILED_URL',
        'SEVERPAY_RETURN_URL',
        'CRYPT4_REDIRECT_URL',
        'TELEGRAM_WEBHOOK_SECRET',
        'PANEL_WEBHOOK_SECRET',
        'PRIVACY_POLICY_URL',
        mode='before',
    )
    @classmethod
    def sanitize_optional_link(cls, v):
        if isinstance(v, str) and not v.strip():
            return None
        return v
    
    @field_validator(
        'REQUIRED_CHANNEL_ID',
        'FREEKASSA_PAYMENT_METHOD_ID',
        'USER_HWID_DEVICE_LIMIT',
        'SEVERPAY_MID',
        'SEVERPAY_LIFETIME_MINUTES',
        'LOG_CHAT_ID',
        'LOG_THREAD_ID',
        'LOG_THREAD_ID_USERS',
        'LOG_THREAD_ID_PURCHASES',
        'LOG_THREAD_ID_STATUSES',
        'LOG_THREAD_ID_BACKUPS',
        'YOOKASSA_TAX_SYSTEM_CODE',
        mode='before'
    )
    @classmethod
    def validate_optional_int(cls, v):
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return None
        return v

    @field_validator('PLATEGA_CLIENT_COMMISSION_PERCENT', mode='before')
    @classmethod
    def validate_platega_client_commission(cls, v):
        if v is None:
            return 0.0
        if isinstance(v, str):
            v = v.strip().replace(",", ".").rstrip("%").strip()
            if not v:
                return 0.0
        return v

    @field_validator('PLATEGA_CLIENT_COMMISSION_PERCENT')
    @classmethod
    def check_platega_client_commission_range(cls, v):
        if v < 0 or v >= 100:
            raise ValueError(
                "PLATEGA_CLIENT_COMMISSION_PERCENT must be a percentage in range [0, 100)."
            )
        return v

    @field_validator('YOOKASSA_PAYMENT_MODE', 'YOOKASSA_PAYMENT_SUBJECT', mode='before')
    @classmethod
    def normalize_optional_yookassa_receipt_fields(cls, v):
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return None
        return v

    @field_validator('YOOKASSA_TAX_SYSTEM_CODE')
    @classmethod
    def validate_yookassa_tax_system_code(cls, v):
        if v is None:
            return None
        if not 1 <= v <= 6:
            raise ValueError("YOOKASSA_TAX_SYSTEM_CODE must be an integer from 1 to 6.")
        return v
    
    # Notification types
    # Topic-specific thread IDs (override LOG_THREAD_ID per event category)
    LOG_THREAD_ID_USERS: Optional[int] = Field(
        default=None,
        description="Topic thread ID for user events (registrations, trials)"
    )
    LOG_THREAD_ID_PURCHASES: Optional[int] = Field(
        default=None,
        description="Topic thread ID for purchases (payments, promo codes)"
    )
    LOG_THREAD_ID_STATUSES: Optional[int] = Field(
        default=None,
        description="Topic thread ID for statuses (panel sync, node alerts)"
    )
    LOG_THREAD_ID_BACKUPS: Optional[int] = Field(
        default=None,
        description="Topic thread ID for database backup reports"
    )

    LOG_NEW_USERS: bool = Field(default=True, description="Send notifications for new user registrations")
    LOG_PAYMENTS: bool = Field(default=True, description="Send notifications for successful payments")
    LOG_PROMO_ACTIVATIONS: bool = Field(default=True, description="Send notifications for promo code activations")
    LOG_TRIAL_ACTIVATIONS: bool = Field(default=True, description="Send notifications for trial activations")
    LOG_SUSPICIOUS_ACTIVITY: bool = Field(default=True, description="Send notifications for suspicious promo attempts")

    # Database backup settings
    BACKUP_PASSWORD: str = Field(default="changeme", description="Password for encrypted backup archives")
    BACKUP_HOUR: int = Field(default=9, description="Hour (local time) to run the daily database backup (0-23)")

    DISCOUNT_PROMO_PAYMENT_TIMEOUT_MINUTES: int = Field(
        default=10,
        description="How long a discount promo reservation is kept before user payment",
    )

    # Auto sync with panel
    AUTO_SYNC_ENABLED: bool = Field(default=False, description="Enable automatic periodic panel synchronization")
    AUTO_SYNC_INTERVAL_HOURS: int = Field(default=6, description="How often (hours) to run automatic panel sync")

    model_config = SettingsConfigDict(env_file='.env',
                                      hide_input_in_errors=True,
                                      env_file_encoding='utf-8',
                                      extra='ignore',
                                      populate_by_name=True)


_settings_instance: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings_instance
    if _settings_instance is None:
        try:
            _settings_instance = Settings()
            if not _settings_instance.ADMIN_IDS:
                logging.warning(
                    "CRITICAL: ADMIN_IDS not set or contains no valid integer IDs in .env. "
                    "Admin functionality will be restricted.")

            if not _settings_instance.PANEL_API_URL:
                logging.warning(
                    "CRITICAL: PANEL_API_URL is not set. Panel integration will not work."
                )
            if _settings_instance.WEBHOOK_BASE_URL and not _settings_instance.TELEGRAM_WEBHOOK_SECRET:
                logging.warning(
                    "WARNING: TELEGRAM_WEBHOOK_SECRET is empty while webhook mode is enabled. "
                    "Set TELEGRAM_WEBHOOK_SECRET to validate X-Telegram-Bot-Api-Secret-Token header."
                )
            if not _settings_instance.YOOKASSA_SHOP_ID or not _settings_instance.YOOKASSA_SECRET_KEY:
                logging.warning(
                    "CRITICAL: YooKassa credentials (SHOP_ID or SECRET_KEY) are not set. Payments will not work."
                )
            if (
                _settings_instance.LKNPD_INN
                or _settings_instance.LKNPD_PASSWORD
            ) and not (
                _settings_instance.LKNPD_INN
                and _settings_instance.LKNPD_PASSWORD
            ):
                logging.warning(
                    "WARNING: LKNPD credentials are incomplete. Receipt sending will be disabled."
                )
            if _settings_instance.FREEKASSA_ENABLED:
                if (
                    not _settings_instance.FREEKASSA_MERCHANT_ID
                    or not _settings_instance.FREEKASSA_API_KEY
                ):
                    logging.warning(
                        "CRITICAL: FreeKassa is enabled but SHOP_ID or API key is missing. FreeKassa payments will not work."
                    )
                if not _settings_instance.FREEKASSA_SECOND_SECRET:
                    logging.warning(
                        "WARNING: FreeKassa second secret is not set. Incoming payment notifications cannot be verified."
                    )

            if _settings_instance.PLATEGA_ENABLED:
                if (
                    not _settings_instance.PLATEGA_MERCHANT_ID
                    or not _settings_instance.PLATEGA_SECRET
                ):
                    logging.warning(
                        "CRITICAL: Platega is enabled but merchant credentials (PLATEGA_MERCHANT_ID/PLATEGA_SECRET) are missing. Platega payments will not work."
                    )
            if _settings_instance.CISPAY_ENABLED:
                if (
                    not _settings_instance.CISPAY_SHOP_ID
                    or not _settings_instance.CISPAY_API_KEY
                ):
                    logging.warning(
                        "CRITICAL: cisPay is enabled but merchant credentials (CISPAY_SHOP_ID/CISPAY_API_KEY) are missing. cisPay payments will not work."
                    )
            if _settings_instance.PAY2328_ENABLED:
                if (
                    not _settings_instance.PAY2328_PROJECT_ID
                    or not _settings_instance.PAY2328_API_KEY
                ):
                    logging.warning(
                        "CRITICAL: 2328 is enabled but PAY2328_PROJECT_ID/PAY2328_API_KEY are missing. 2328 payments will not work."
                    )
                if not _settings_instance.WEBHOOK_BASE_URL:
                    logging.warning(
                        "CRITICAL: 2328 is enabled but WEBHOOK_BASE_URL is not set. 2328 cannot deliver payment callbacks, so subscriptions will never activate."
                    )
            if _settings_instance.SEVERPAY_ENABLED:
                if not _settings_instance.SEVERPAY_MID or not _settings_instance.SEVERPAY_TOKEN:
                    logging.warning(
                        "CRITICAL: SeverPay is enabled but MID or TOKEN is missing. SeverPay payments will not work."
                    )

        except ValidationError as e:
            logging.critical(
                f"Pydantic validation error while loading settings: {e}")

            raise SystemExit(
                f"CRITICAL SETTINGS ERROR: {e}. Please check your .env file and Settings model."
            )
    return _settings_instance
