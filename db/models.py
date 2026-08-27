from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, Float, ForeignKey, UniqueConstraint, Text, BigInteger
from sqlalchemy.orm import relationship, DeclarativeBase
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.sql import func
from datetime import datetime


class Base(AsyncAttrs, DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    user_id = Column(BigInteger, primary_key=True, index=True)
    username = Column(String, nullable=True, index=True)
    first_name = Column(String, nullable=True)
    last_name = Column(String, nullable=True)
    language_code = Column(String, default="ru")
    registration_date = Column(DateTime(timezone=True),
                               server_default=func.now())
    is_banned = Column(Boolean, default=False)
    panel_user_uuid = Column(String, nullable=True, unique=True, index=True)
    referral_code = Column(String(16), nullable=True, unique=True, index=True)
    referred_by_id = Column(BigInteger,
                            ForeignKey("users.user_id"),
                            nullable=True)
    channel_subscription_verified = Column(Boolean, nullable=True)
    channel_subscription_checked_at = Column(DateTime(timezone=True),
                                             nullable=True)
    channel_subscription_verified_for = Column(BigInteger, nullable=True)

    referrer = relationship("User", remote_side=[user_id], backref="referrals")
    subscriptions = relationship("Subscription",
                                 back_populates="user",
                                 cascade="all, delete-orphan")
    payments = relationship("Payment",
                            back_populates="user",
                            cascade="all, delete-orphan")
    promo_code_activations = relationship("PromoCodeActivation",
                                          back_populates="user",
                                          cascade="all, delete-orphan")
    message_logs_authored = relationship("MessageLog",
                                         foreign_keys="MessageLog.user_id",
                                         back_populates="author_user",
                                         cascade="all, delete-orphan")
    message_logs_targeted = relationship(
        "MessageLog",
        foreign_keys="MessageLog.target_user_id",
        back_populates="target_user",
        cascade="all, delete-orphan")

    def __repr__(self):
        return f"<User(user_id={self.user_id}, username='{self.username}')>"


class Subscription(Base):
    __tablename__ = "subscriptions"

    subscription_id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger,
                     ForeignKey("users.user_id"),
                     nullable=False,
                     index=True)
    panel_user_uuid = Column(String, nullable=False, index=True)
    panel_subscription_uuid = Column(String,
                                     unique=True,
                                     index=True,
                                     nullable=True)
    start_date = Column(DateTime(timezone=True), nullable=True)
    end_date = Column(DateTime(timezone=True), nullable=False, index=True)
    duration_months = Column(Integer, nullable=True)
    is_active = Column(Boolean, default=True, index=True)
    status_from_panel = Column(String, nullable=True)
    traffic_limit_bytes = Column(BigInteger, nullable=True)
    traffic_used_bytes = Column(BigInteger, nullable=True)
    last_notification_sent = Column(DateTime(timezone=True), nullable=True)
    provider = Column(String, nullable=True)
    skip_notifications = Column(Boolean, default=False)
    auto_renew_enabled = Column(Boolean, default=True, index=True)
    # Device tier (hwidDeviceLimit) this subscription was purchased at.
    # NULL on legacy rows -> treated as the base device tier at read time.
    hwid_device_limit = Column(Integer, nullable=True)

    user = relationship("User", back_populates="subscriptions")

    def __repr__(self):
        return f"<Subscription(id={self.subscription_id}, user_id={self.user_id}, panel_uuid='{self.panel_user_uuid}', ends='{self.end_date}')>"


class Payment(Base):
    __tablename__ = "payments"

    payment_id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger,
                     ForeignKey("users.user_id"),
                     nullable=False,
                     index=True)
    yookassa_payment_id = Column(String,
                                 unique=True,
                                 index=True,
                                 nullable=True)
    provider_payment_id = Column(String, unique=True, nullable=True)
    provider = Column(String, nullable=False, default="yookassa", index=True)
    idempotence_key = Column(String, unique=True, nullable=True)
    amount = Column(Float, nullable=False)  # Final amount paid (after discount if any)

    # Discount tracking fields
    original_amount = Column(Float, nullable=True)  # Amount before discount
    discount_applied = Column(Float, nullable=True)  # Discount amount (not percentage)

    currency = Column(String, nullable=False)
    # Value of `amount` in the base currency (PARTNER_PAYOUT_CURRENCY), frozen
    # with the rate that produced it at the moment of purchase. NULL means the
    # currency had no configured rate yet — such a payment is left out of every
    # money total rather than counted at face value.
    base_amount = Column(Float, nullable=True)
    fx_rate = Column(Float, nullable=True)
    status = Column(String, nullable=False, index=True)
    description = Column(String, nullable=True)
    subscription_duration_months = Column(Integer, nullable=True)
    # Device tier (hwidDeviceLimit) this payment was made for, if applicable.
    hwid_device_limit = Column(Integer, nullable=True)
    promo_code_id = Column(Integer,
                           ForeignKey("promo_codes.promo_code_id"),
                           nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True),
                        onupdate=func.now(),
                        nullable=True)

    user = relationship("User", back_populates="payments")
    promo_code_used = relationship("PromoCode",
                                   back_populates="payments_where_used")


class UserBilling(Base):
    __tablename__ = "user_billing"

    user_id = Column(BigInteger, ForeignKey("users.user_id"), primary_key=True)
    # Saved payment method for off-session recurring charges (YooKassa)
    yookassa_payment_method_id = Column(String, nullable=True, unique=True)
    card_last4 = Column(String, nullable=True)
    card_network = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), nullable=True)

    user = relationship("User")

class UserPaymentMethod(Base):
    __tablename__ = "user_payment_methods"

    method_id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.user_id"), nullable=False, index=True)
    provider = Column(String, nullable=False, default="yookassa", index=True)
    provider_payment_method_id = Column(String, nullable=False, unique=True, index=True)
    card_last4 = Column(String, nullable=True)
    card_network = Column(String, nullable=True)
    is_default = Column(Boolean, default=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), nullable=True)

    user = relationship("User")
    __table_args__ = (
        UniqueConstraint('user_id', 'provider_payment_method_id', name='uq_user_provider_method'),
    )

class PromoCode(Base):
    __tablename__ = "promo_codes"

    promo_code_id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String, unique=True, nullable=False, index=True)

    # Type field to distinguish promo code types
    promo_type = Column(String, nullable=False, default="bonus_days", index=True)
    # Values: "bonus_days" or "discount"

    # For bonus_days type: number of days to add to subscription
    bonus_days = Column(Integer, nullable=True)

    # For discount type: percentage discount (1-100)
    discount_percentage = Column(Integer, nullable=True)

    max_activations = Column(Integer, nullable=False)
    current_activations = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    created_by_admin_id = Column(BigInteger, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    valid_until = Column(DateTime(timezone=True), nullable=True)

    activations = relationship("PromoCodeActivation",
                               back_populates="promo_code",
                               cascade="all, delete-orphan")
    payments_where_used = relationship("Payment",
                                       back_populates="promo_code_used")


class PromoCodeActivation(Base):
    __tablename__ = "promo_code_activations"

    activation_id = Column(Integer, primary_key=True, autoincrement=True)
    promo_code_id = Column(Integer,
                           ForeignKey("promo_codes.promo_code_id"),
                           nullable=False)
    user_id = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    activated_at = Column(DateTime(timezone=True), server_default=func.now())
    payment_id = Column(Integer,
                        ForeignKey("payments.payment_id"),
                        nullable=True)

    promo_code = relationship("PromoCode", back_populates="activations")
    user = relationship("User", back_populates="promo_code_activations")
    payment = relationship("Payment")

    __table_args__ = (UniqueConstraint('promo_code_id',
                                       'user_id',
                                       name='uq_promo_user_activation'), )


class ActiveDiscount(Base):
    """Tracks pending discount promo code reservations awaiting payment."""
    __tablename__ = "active_discounts"

    user_id = Column(
        BigInteger,
        ForeignKey("users.user_id", ondelete="CASCADE"),
        primary_key=True,
    )
    promo_code_id = Column(
        Integer,
        ForeignKey("promo_codes.promo_code_id", ondelete="CASCADE"),
        nullable=False,
    )
    discount_percentage = Column(Integer, nullable=False)
    activated_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)

    promo_code = relationship("PromoCode")
    user = relationship("User")


class MessageLog(Base):
    __tablename__ = "message_logs"

    log_id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger,
                     ForeignKey("users.user_id"),
                     nullable=True,
                     index=True)
    telegram_username = Column(String, nullable=True)
    telegram_first_name = Column(String, nullable=True)
    event_type = Column(String, nullable=False, index=True)
    content = Column(Text, nullable=True)
    raw_update_preview = Column(Text, nullable=True)
    timestamp = Column(DateTime(timezone=True),
                       server_default=func.now(),
                       index=True)
    is_admin_event = Column(Boolean, default=False)
    target_user_id = Column(BigInteger,
                            ForeignKey("users.user_id"),
                            nullable=True,
                            index=True)

    author_user = relationship("User",
                               foreign_keys=[user_id],
                               back_populates="message_logs_authored")
    target_user = relationship("User",
                               foreign_keys=[target_user_id],
                               back_populates="message_logs_targeted")


class PanelSyncStatus(Base):
    __tablename__ = "panel_sync_status"

    id = Column(Integer, primary_key=True, default=1, autoincrement=False)
    last_sync_time = Column(DateTime(timezone=True), nullable=True)
    status = Column(String, nullable=True)
    details = Column(Text, nullable=True)
    users_processed_from_panel = Column(Integer, default=0)
    subscriptions_synced = Column(Integer, default=0)

    __table_args__ = (UniqueConstraint('id'), )


CAMPAIGN_TYPE_AD = "ad"
CAMPAIGN_TYPE_PARTNER = "partner"


class AdCampaign(Base):
    __tablename__ = "ad_campaigns"

    ad_campaign_id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String, nullable=False, index=True)
    start_param = Column(String, nullable=False, unique=True, index=True)
    cost = Column(Float, nullable=False, default=0.0)
    is_active = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # "ad" - paid traffic we bought ourselves (cost is what we spent).
    # "partner" - affiliate label owned by partner_user_id, who earns
    # partner_percent of the revenue their users generate.
    campaign_type = Column(
        String(16),
        nullable=False,
        server_default=CAMPAIGN_TYPE_AD,
        default=CAMPAIGN_TYPE_AD,
        index=True,
    )
    partner_user_id = Column(
        BigInteger, ForeignKey("users.user_id"), nullable=True, index=True
    )
    # Immutable once set: accrued earnings are recomputed from payments on
    # every read, so changing it would rewrite the partner's history.
    partner_percent = Column(Float, nullable=True)

    attributions = relationship(
        "AdAttribution",
        back_populates="campaign",
        cascade="all, delete-orphan",
    )
    partner_user = relationship("User", foreign_keys=[partner_user_id])
    payouts = relationship(
        "PartnerPayout",
        back_populates="campaign",
        cascade="all, delete-orphan",
    )
    accruals = relationship(
        "CampaignAccrual",
        back_populates="campaign",
        cascade="all, delete-orphan",
    )

    @property
    def is_partner(self) -> bool:
        return self.campaign_type == CAMPAIGN_TYPE_PARTNER

    def __repr__(self):
        return f"<AdCampaign(id={self.ad_campaign_id}, type='{self.campaign_type}', source='{self.source}', start_param='{self.start_param}', cost={self.cost})>"


class AdAttribution(Base):
    __tablename__ = "ad_attributions"

    user_id = Column(BigInteger, ForeignKey("users.user_id"), primary_key=True, index=True)
    ad_campaign_id = Column(Integer, ForeignKey("ad_campaigns.ad_campaign_id"), nullable=False, index=True)
    first_start_at = Column(DateTime(timezone=True), server_default=func.now())
    trial_activated_at = Column(DateTime(timezone=True), nullable=True)

    user = relationship("User")
    campaign = relationship("AdCampaign", back_populates="attributions")


class PartnerPayout(Base):
    __tablename__ = "partner_payouts"

    payout_id = Column(Integer, primary_key=True, autoincrement=True)
    ad_campaign_id = Column(
        Integer,
        ForeignKey("ad_campaigns.ad_campaign_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    amount = Column(Float, nullable=False)
    currency = Column(String, nullable=False, default="RUB")
    # Rate applied when the payout was recorded, and the resulting base-currency
    # value. Frozen for the same reason accruals are: a later rate change must
    # not silently rewrite what a partner was already paid.
    fx_rate = Column(Float, nullable=False, default=1.0)
    base_amount = Column(Float, nullable=False, default=0.0)
    comment = Column(String, nullable=True)
    created_by = Column(BigInteger, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    campaign = relationship("AdCampaign", back_populates="payouts")

    def __repr__(self):
        return (
            f"<PartnerPayout(id={self.payout_id}, campaign={self.ad_campaign_id}, "
            f"amount={self.amount} {self.currency} -> {self.base_amount})>"
        )


class CampaignAccrual(Base):
    """One row per attributed payment — the campaign revenue ledger.

    Written once, when a payment reaches "succeeded" (or when a reader notices
    an eligible payment without a row yet), and never recomputed. It does no
    currency math: `base_amount` is copied from the payment, which was
    normalised at purchase time. What the ledger adds is the campaign side of
    the deal — the partner share in force at that moment and the resulting
    `earned_amount` — so a later percent change cannot rewrite history.
    """

    __tablename__ = "campaign_accruals"

    accrual_id = Column(Integer, primary_key=True, autoincrement=True)
    ad_campaign_id = Column(
        Integer,
        ForeignKey("ad_campaigns.ad_campaign_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Unique: a payment can never be accrued twice. Nulled rather than deleted
    # when a user is purged, so the money history outlives the customer record.
    payment_id = Column(
        Integer,
        ForeignKey("payments.payment_id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
        index=True,
    )
    # Snapshot, deliberately without a FK for the same reason.
    user_id = Column(BigInteger, nullable=False, index=True)

    amount = Column(Float, nullable=False)
    currency = Column(String, nullable=False)
    # Copied from the payment, which is where the conversion happens. Kept here
    # rather than joined so the ledger survives a purged payment and so that
    # campaign totals are a single-table SUM.
    base_amount = Column(Float, nullable=False)

    percent = Column(Float, nullable=False, default=0.0)
    earned_amount = Column(Float, nullable=False, default=0.0)

    provider = Column(String, nullable=True)
    subscription_duration_months = Column(Integer, nullable=True)
    hwid_device_limit = Column(Integer, nullable=True)

    # Snapshot of payments.created_at — the moment the invoice was created,
    # which is the timestamp campaign windows have always been measured on.
    paid_at = Column(DateTime(timezone=True), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    campaign = relationship("AdCampaign", back_populates="accruals")

    def __repr__(self):
        return (
            f"<CampaignAccrual(id={self.accrual_id}, campaign={self.ad_campaign_id}, "
            f"payment={self.payment_id}, {self.amount} {self.currency} -> {self.base_amount}, "
            f"earned={self.earned_amount})>"
        )


class CurrencyRate(Base):
    """Admin-managed conversion rates into the base currency.

    The database is the source of truth: PARTNER_CURRENCY_RATES only seeds this
    table on first migration. A currency absent here has no rate, and payments
    in it stay unvalued (`payments.base_amount IS NULL`) until an admin adds
    one — deliberately, so nothing is ever counted at face value.
    """

    __tablename__ = "currency_rates"

    currency = Column(String(16), primary_key=True)
    rate = Column(Float, nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    updated_by = Column(BigInteger, nullable=True)

    def __repr__(self):
        return f"<CurrencyRate({self.currency}={self.rate})>"
