"""Panel linkage in SubscriptionService after the 2.x -> 3.x identity change.

Every `panel_user_uuid` stored while running a 2.x panel is stale once the panel
drops `User.uuid`, so the bot has to re-resolve the user and carry the existing
subscription rows over to the new numeric reference.
"""

from types import SimpleNamespace

import pytest

from bot.services import subscription_service as ss
from bot.services.subscription_service import SubscriptionService
from tests.conftest import panel_user

TG_ID = 111
NEW_REF = "42"
LEGACY_REF = "2f9a-legacy-uuid"


class FakePanelService:
    def __init__(
        self, by_telegram=None, by_username=None, by_ref=None, created=None,
        username_sequence=None,
    ):
        self._by_telegram = by_telegram
        self._by_username = by_username
        self._username_sequence = list(username_sequence) if username_sequence else None
        self._by_ref = by_ref
        self._created = created
        self.calls = []
        self.updates = []

    async def get_users_by_filter(self, telegram_id=None, username=None, email=None, log_response=True):
        if telegram_id is not None:
            self.calls.append(("by_telegram", telegram_id))
            return self._by_telegram
        if username is not None:
            self.calls.append(("by_username", username))
            if self._username_sequence is not None:
                return self._username_sequence.pop(0) if self._username_sequence else []
            return self._by_username
        return []

    async def get_user_by_ref(self, user_ref, log_response=True):
        self.calls.append(("by_ref", user_ref))
        return self._by_ref

    async def create_panel_user(self, **kwargs):
        self.calls.append(("create", kwargs.get("username_on_panel")))
        return self._created

    async def update_user_details_on_panel(self, user_ref, payload, log_response=True):
        self.updates.append((user_ref, payload))
        return payload


@pytest.fixture
def settings():
    return SimpleNamespace(
        parsed_user_squad_uuids=None,
        parsed_user_external_squad_uuid=None,
        user_traffic_limit_bytes=0,
        USER_TRAFFIC_STRATEGY="NO_RESET",
        DEFAULT_LANGUAGE="ru",
        ADMIN_IDS=[],
        USER_HWID_DEVICE_LIMIT=None,
    )


@pytest.fixture
def dal_spy(monkeypatch):
    """Stub the DAL and record what the service writes."""
    state = {"user_updates": [], "rebinds": [], "conflict_user": None}

    async def get_user_by_panel_uuid(session, ref):
        return state["conflict_user"]

    async def update_user(session, user_id, payload):
        state["user_updates"].append((user_id, payload))
        return None

    async def rebind_panel_user_reference(session, user_id, new_ref):
        state["rebinds"].append((user_id, new_ref))
        return 1

    monkeypatch.setattr(ss.user_dal, "get_user_by_panel_uuid", get_user_by_panel_uuid)
    monkeypatch.setattr(ss.user_dal, "update_user", update_user)
    monkeypatch.setattr(
        ss.subscription_dal, "rebind_panel_user_reference", rebind_panel_user_reference
    )
    return state


def make_db_user(panel_ref):
    return SimpleNamespace(
        user_id=TG_ID,
        panel_user_uuid=panel_ref,
        username="tester",
        first_name="Test",
        last_name=None,
    )


async def link(settings, panel_service, db_user):
    service = SubscriptionService(settings=settings, panel_service=panel_service)
    return await service._get_or_create_panel_user_link_details(
        session=object(), user_id=TG_ID, db_user=db_user
    )


async def test_stale_uuid_is_replaced_by_numeric_id(settings, dal_spy):
    """The panel is found by telegramId and the local reference is rewritten."""
    panel = FakePanelService(by_telegram=[panel_user(user_id=42, telegram_id=TG_ID)])
    db_user = make_db_user(LEGACY_REF)

    ref, sub_link_id, short_uuid, linked_now = await link(settings, panel, db_user)

    assert ref == NEW_REF
    assert linked_now is True
    assert dal_spy["user_updates"] == [(TG_ID, {"panel_user_uuid": NEW_REF})]
    assert short_uuid == "short-abc"
    assert sub_link_id == "short-abc"


async def test_stale_uuid_rebinds_existing_subscriptions(settings, dal_spy):
    """Without this, reference-filtered lookups lose the user's active sub."""
    panel = FakePanelService(by_telegram=[panel_user(user_id=42, telegram_id=TG_ID)])

    await link(settings, panel, make_db_user(LEGACY_REF))

    assert dal_spy["rebinds"] == [(TG_ID, NEW_REF)]


async def test_unchanged_reference_does_not_rebind(settings, dal_spy):
    panel = FakePanelService(by_telegram=[panel_user(user_id=42, telegram_id=TG_ID)])

    await link(settings, panel, make_db_user(NEW_REF))

    assert dal_spy["rebinds"] == []
    assert dal_spy["user_updates"] == []


async def test_falls_back_to_username_before_creating_a_duplicate(settings, dal_spy):
    """telegramId cleared on the panel must not produce a second panel user."""
    panel = FakePanelService(
        by_telegram=[],
        by_username=[panel_user(user_id=42, telegram_id=None, username="tg_111")],
    )

    ref, _, _, _ = await link(settings, panel, make_db_user(LEGACY_REF))

    assert ref == NEW_REF
    assert ("create", "tg_111") not in panel.calls
    assert panel.calls[:2] == [("by_telegram", TG_ID), ("by_username", "tg_111")]


async def test_creates_panel_user_when_nothing_matches(settings, dal_spy):
    panel = FakePanelService(
        by_telegram=[],
        by_username=[],
        by_ref=None,
        created={"response": panel_user(user_id=99, telegram_id=TG_ID)},
    )

    ref, _, _, linked_now = await link(settings, panel, make_db_user(LEGACY_REF))

    assert ref == "99"
    assert linked_now is True
    assert ("create", "tg_111") in panel.calls


async def test_no_local_reference_creates_panel_user(settings, dal_spy):
    panel = FakePanelService(
        by_telegram=[],
        by_username=[],
        created={"response": panel_user(user_id=7, telegram_id=TG_ID)},
    )

    ref, _, _, _ = await link(settings, panel, make_db_user(None))

    assert ref == "7"
    assert dal_spy["user_updates"] == [(TG_ID, {"panel_user_uuid": "7"})]


async def test_username_conflict_on_create_is_recovered(settings, dal_spy):
    """A019 = USER_USERNAME_ALREADY_EXISTS arrives nested under `details`."""
    panel = FakePanelService(
        by_telegram=[],
        username_sequence=[[], [panel_user(user_id=42, telegram_id=TG_ID)]],
        created={
            "error": True,
            "status_code": 400,
            "details": {"errorCode": "A019", "message": "User username already exists"},
        },
    )

    ref, _, _, _ = await link(settings, panel, make_db_user(None))

    assert ref == NEW_REF
    assert [c[0] for c in panel.calls] == ["by_telegram", "by_username", "create", "by_username"]


async def test_multiple_panel_users_for_one_telegram_id_aborts(settings, dal_spy):
    panel = FakePanelService(
        by_telegram=[panel_user(user_id=1, telegram_id=TG_ID), panel_user(user_id=2, telegram_id=TG_ID)]
    )

    result = await link(settings, panel, make_db_user(LEGACY_REF))

    assert result == (None, None, None, False)
    assert dal_spy["rebinds"] == []


async def test_reference_already_owned_by_another_user_aborts(settings, dal_spy):
    """Refuse to steal a panel reference that another Telegram user is linked to."""
    dal_spy["conflict_user"] = SimpleNamespace(user_id=999)
    panel = FakePanelService(by_telegram=[panel_user(user_id=42, telegram_id=TG_ID)])

    result = await link(settings, panel, make_db_user(LEGACY_REF))

    assert result == (None, None, None, False)
    assert dal_spy["user_updates"] == []
    assert dal_spy["rebinds"] == []


async def test_missing_telegram_id_on_panel_is_pushed_back(settings, dal_spy):
    panel = FakePanelService(
        by_telegram=[], by_username=[panel_user(user_id=42, telegram_id=None)]
    )

    await link(settings, panel, make_db_user(NEW_REF))

    assert panel.updates, "expected the panel user to be updated with telegramId"
    ref, payload = panel.updates[-1]
    assert ref == NEW_REF
    assert payload["telegramId"] == TG_ID


async def test_panel_update_payload_carries_no_uuid(settings):
    """PATCH bodies must not smuggle a `uuid` key; 3.x rejects unknown identity."""
    service = SubscriptionService(settings=settings, panel_service=FakePanelService())

    payload = service._build_panel_update_payload(
        panel_user_uuid=LEGACY_REF,
        status="ACTIVE",
        hwid_device_limit=5,
    )

    assert "uuid" not in payload
    assert "id" not in payload
    assert payload["status"] == "ACTIVE"
    assert payload["hwidDeviceLimit"] == 5
