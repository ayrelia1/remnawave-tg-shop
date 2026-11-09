import re
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, F, Router
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import LabeledPrice, Message, PreCheckoutQuery, Update, User

from bot.filters.admin_filter import AdminFilter
from bot.middlewares.i18n import JsonI18n
from config.settings import Settings


PAYLOAD_PREFIX = "admin_stars:"
MAX_INVOICE_AMOUNT = 2**63 - 1


def _language(user: User) -> str:
    return (user.language_code or "").split("-", 1)[0].lower()


async def send_stars_invoice(
    message: Message,
    command: CommandObject,
    event_from_user: User,
    i18n_instance: JsonI18n,
) -> None:
    language = _language(event_from_user)
    raw_amount = (command.args or "").strip().lstrip("0")
    amount = int(raw_amount) if re.fullmatch(r"[0-9]{1,19}", raw_amount) else 0
    if not 0 < amount <= MAX_INVOICE_AMOUNT:
        await message.answer(i18n_instance.gettext(language, "admin_stars_usage"))
        return

    title = i18n_instance.gettext(language, "admin_stars_title")
    try:
        await message.answer_invoice(
            title=title,
            description=i18n_instance.gettext(language, "admin_stars_description"),
            payload=f"{PAYLOAD_PREFIX}{event_from_user.id}:{amount}",
            currency="XTR",
            prices=[LabeledPrice(label=title, amount=amount)],
            start_parameter="stars-topup",
        )
    except TelegramAPIError:
        await message.answer(i18n_instance.gettext(language, "error_payment_gateway"))


async def answer_stars_checkout(
    query: PreCheckoutQuery, settings: Settings, i18n_instance: JsonI18n
) -> None:
    expected_payload = f"{PAYLOAD_PREFIX}{query.from_user.id}:{query.total_amount}"
    ok = (
        query.from_user.id in settings.ADMIN_IDS
        and query.currency == "XTR"
        and 0 < query.total_amount <= MAX_INVOICE_AMOUNT
        and query.invoice_payload == expected_payload
    )
    if ok:
        await query.answer(ok=True)
    else:
        await query.answer(
            ok=False,
            error_message=i18n_instance.gettext(_language(query.from_user), "admin_stars_checkout_error"),
        )


async def ignore_stars_payment(message: Message) -> None:
    # Telegram records the transfer and sends the receipt.
    return


class AdminStarsMiddleware(BaseMiddleware):
    """Handle admin top-ups before the database-backed middleware chain."""

    def __init__(self, settings: Settings):
        self.router = Router(name="admin_stars_router")
        self.router.message.filter(F.chat.type == "private")
        self.router.message.register(
            send_stars_invoice, F.text, AdminFilter(settings.ADMIN_IDS), Command("stars")
        )
        self.router.pre_checkout_query.register(
            answer_stars_checkout, F.invoice_payload.startswith(PAYLOAD_PREFIX)
        )
        self.router.message.register(
            ignore_stars_payment, F.successful_payment.invoice_payload.startswith(PAYLOAD_PREFIX)
        )
        self.router.message.register(
            ignore_stars_payment, F.refunded_payment.invoice_payload.startswith(PAYLOAD_PREFIX)
        )

    async def __call__(
        self,
        handler: Callable[[Update, dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: dict[str, Any],
    ) -> Any:
        if event.message is not None or event.pre_checkout_query is not None:
            result = await self.router.propagate_event(event.event_type, event.event, **data)
            if result is not UNHANDLED:
                return result
        return await handler(event, data)
