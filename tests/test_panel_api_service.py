"""PanelApiService must speak the Remnawave 3.2.1 REST contract.

Route and payload expectations are taken from
libs/contract/api/controllers/{users,hwid,system}.ts at tag 3.2.1.
"""

import pytest

from bot.services.panel_api_service import (
    PanelUnavailableError,
    extract_panel_user_ref,
    panel_user_id,
    panel_username_for_telegram_id,
)
from tests.conftest import RecordingPanelApiService, api_error, empty_body, ok, panel_user


# --------------------------------------------------------------------------
# Identity helpers
# --------------------------------------------------------------------------

def test_extract_ref_prefers_numeric_id():
    assert extract_panel_user_ref({"id": 42}) == "42"


def test_extract_ref_falls_back_to_legacy_uuid():
    assert extract_panel_user_ref({"uuid": "0e5a-legacy"}) == "0e5a-legacy"


def test_extract_ref_prefers_id_when_both_present():
    assert extract_panel_user_ref({"id": 7, "uuid": "0e5a-legacy"}) == "7"


def test_extract_ref_handles_id_zero():
    assert extract_panel_user_ref({"id": 0}) == "0"


@pytest.mark.parametrize("value", [None, {}, [], "nope"])
def test_extract_ref_returns_none_for_unusable_input(value):
    assert extract_panel_user_ref(value) is None


@pytest.mark.parametrize(
    ("ref", "expected"),
    [("42", 42), (" 42 ", 42), (42, 42), ("0e5a-legacy", None), (None, None), ("", None)],
)
def test_panel_user_id_coercion(ref, expected):
    assert panel_user_id(ref) == expected


def test_panel_username_convention():
    assert panel_username_for_telegram_id(111) == "tg_111"


# --------------------------------------------------------------------------
# GET /api/users/{userId}
# --------------------------------------------------------------------------

async def test_get_user_by_ref_uses_numeric_path(panel_settings):
    panel = RecordingPanelApiService(panel_settings, [ok(panel_user(user_id=42))])

    user = await panel.get_user_by_ref("42")

    assert panel.last_call[:2] == ("GET", "/users/42")
    assert user["id"] == 42


async def test_get_user_by_ref_refuses_legacy_uuid_without_calling_panel(panel_settings):
    """A pre-3.0 UUID is not a valid userId; sending it would 400."""
    panel = RecordingPanelApiService(panel_settings, [ok(panel_user())])

    assert await panel.get_user_by_ref("2f9a-legacy-uuid") is None
    assert panel.calls == []


async def test_get_user_by_ref_raises_on_5xx(panel_settings):
    panel = RecordingPanelApiService(panel_settings, [api_error(503)])

    with pytest.raises(PanelUnavailableError):
        await panel.get_user_by_ref("42")


# --------------------------------------------------------------------------
# GET /api/users/stream  (replaces the removed by-telegram-id / by-email routes)
# --------------------------------------------------------------------------

async def test_lookup_by_telegram_id_uses_stream_filter(panel_settings):
    panel = RecordingPanelApiService(
        panel_settings,
        [ok({"users": [panel_user(telegram_id=111)], "nextCursor": None, "hasMore": False})],
    )

    users = await panel.get_users_by_filter(telegram_id=111)

    method, endpoint, kwargs = panel.last_call
    assert (method, endpoint) == ("GET", "/users/stream")
    assert kwargs["params"]["telegramId"] == 111
    assert "start" not in kwargs["params"]
    assert len(users) == 1


async def test_lookup_by_telegram_id_never_hits_removed_route(panel_settings):
    panel = RecordingPanelApiService(
        panel_settings, [ok({"users": [], "nextCursor": None, "hasMore": False})]
    )

    await panel.get_users_by_filter(telegram_id=111)

    assert all("by-telegram-id" not in endpoint for _, endpoint, _ in panel.calls)


async def test_lookup_by_email_uses_stream_filter(panel_settings):
    panel = RecordingPanelApiService(
        panel_settings,
        [ok({"users": [panel_user()], "nextCursor": None, "hasMore": False})],
    )

    await panel.get_users_by_filter(email="a@b.co")

    method, endpoint, kwargs = panel.last_call
    assert (method, endpoint) == ("GET", "/users/stream")
    assert kwargs["params"]["email"] == "a@b.co"
    assert all("by-email" not in ep for _, ep, _ in panel.calls)


async def test_stream_follows_cursor_until_exhausted(panel_settings):
    panel = RecordingPanelApiService(
        panel_settings,
        [
            ok({"users": [panel_user(user_id=1)], "nextCursor": "c1", "hasMore": True}),
            ok({"users": [panel_user(user_id=2)], "nextCursor": "c2", "hasMore": True}),
            ok({"users": [panel_user(user_id=3)], "nextCursor": None, "hasMore": False}),
        ],
    )

    users = await panel.get_all_panel_users()

    assert [u["id"] for u in users] == [1, 2, 3]
    assert len(panel.calls) == 3
    assert "cursor" not in panel.calls[0][2]["params"]
    assert panel.calls[1][2]["params"]["cursor"] == "c1"
    assert panel.calls[2][2]["params"]["cursor"] == "c2"


async def test_stream_stops_when_cursor_missing_despite_has_more(panel_settings):
    panel = RecordingPanelApiService(
        panel_settings,
        [ok({"users": [panel_user()], "nextCursor": None, "hasMore": True})],
    )

    users = await panel.get_all_panel_users()

    assert len(users) == 1
    assert len(panel.calls) == 1


async def test_stream_treats_users_not_found_as_empty(panel_settings):
    panel = RecordingPanelApiService(panel_settings, [api_error(404, "A062")])

    assert await panel.get_users_by_filter(telegram_id=111) == []


async def test_stream_returns_none_on_real_failure(panel_settings):
    panel = RecordingPanelApiService(panel_settings, [api_error(400, "A001")])

    assert await panel.get_users_by_filter(telegram_id=111) is None


# --------------------------------------------------------------------------
# GET /api/users/by-username/{username}  (still present in 3.x)
# --------------------------------------------------------------------------

async def test_lookup_by_username_wraps_single_object(panel_settings):
    panel = RecordingPanelApiService(panel_settings, [ok(panel_user(username="tg_111"))])

    users = await panel.get_users_by_filter(username="tg_111")

    assert panel.last_call[:2] == ("GET", "/users/by-username/tg_111")
    assert [u["username"] for u in users] == ["tg_111"]


@pytest.mark.parametrize("error_code", ["A062", "A025", "A063"])
async def test_lookup_by_username_maps_not_found_to_empty(panel_settings, error_code):
    panel = RecordingPanelApiService(panel_settings, [api_error(404, error_code)])

    assert await panel.get_users_by_filter(username="tg_111") == []


async def test_lookup_by_username_handles_a063(panel_settings):
    """A063 is what by-username actually returns for an unknown user.

    getUserByUniqueFields fails with GET_USER_BY_UNIQUE_FIELDS_NOT_FOUND, not
    A025/A062 — mapping it to a hard error made every first-time provisioning
    log an ERROR before falling through to user creation.
    """
    panel = RecordingPanelApiService(panel_settings, [api_error(404, "A063")])

    assert await panel.get_users_by_filter(username="tg_999") == []


async def test_lookup_by_username_reports_failure_on_other_errors(panel_settings):
    """A non-404 client error is a real failure, not an absent user."""
    panel = RecordingPanelApiService(panel_settings, [api_error(403, "A003")])

    assert await panel.get_users_by_filter(username="tg_111") is None


async def test_lookup_by_username_raises_on_server_error(panel_settings):
    panel = RecordingPanelApiService(panel_settings, [api_error(500, "A064")])

    with pytest.raises(PanelUnavailableError):
        await panel.get_users_by_filter(username="tg_111")


async def test_get_users_by_filter_without_criteria_returns_empty(panel):
    assert await panel.get_users_by_filter() == []
    assert panel.calls == []


# --------------------------------------------------------------------------
# POST /api/users  &  PATCH /api/users
# --------------------------------------------------------------------------

async def test_create_user_payload_matches_contract(panel_settings):
    panel = RecordingPanelApiService(panel_settings, [ok(panel_user())])

    await panel.create_panel_user(
        username_on_panel="tg_111",
        telegram_id=111,
        hwid_device_limit=5,
        specific_squad_uuids=["squad-1"],
        external_squad_uuid="ext-1",
        tag="SHOP",
    )

    method, endpoint, kwargs = panel.last_call
    payload = kwargs["json"]
    assert (method, endpoint) == ("POST", "/users")
    assert payload["username"] == "tg_111"
    assert payload["telegramId"] == 111
    assert payload["hwidDeviceLimit"] == 5
    assert payload["activeInternalSquads"] == ["squad-1"]
    assert payload["externalSquadUuid"] == "ext-1"
    assert payload["tag"] == "SHOP"
    assert "uuid" not in payload


async def test_create_user_rejects_invalid_username_without_calling_panel(panel):
    response = await panel.create_panel_user(username_on_panel="bad name!")

    assert response["errorCode"] == "VALIDATION_ERROR_USERNAME"
    assert panel.calls == []


async def test_update_sends_numeric_id_and_drops_uuid(panel_settings):
    panel = RecordingPanelApiService(panel_settings, [ok(panel_user())])

    await panel.update_user_details_on_panel(
        "42", {"uuid": "2f9a-legacy-uuid", "expireAt": "2026-12-31T00:00:00.000Z"}
    )

    method, endpoint, kwargs = panel.last_call
    payload = kwargs["json"]
    assert (method, endpoint) == ("PATCH", "/users")
    assert payload["id"] == 42
    assert "uuid" not in payload
    assert payload["expireAt"] == "2026-12-31T00:00:00.000Z"


async def test_update_does_not_mutate_the_callers_payload(panel_settings):
    panel = RecordingPanelApiService(panel_settings, [ok(panel_user())])
    original = {"expireAt": "2026-12-31T00:00:00.000Z"}

    await panel.update_user_details_on_panel("42", original)

    assert original == {"expireAt": "2026-12-31T00:00:00.000Z"}


async def test_update_refuses_legacy_uuid_reference(panel_settings):
    panel = RecordingPanelApiService(panel_settings, [ok(panel_user())])

    assert await panel.update_user_details_on_panel("2f9a-legacy", {"status": "ACTIVE"}) is None
    assert panel.calls == []


# --------------------------------------------------------------------------
# POST /api/users/{userId}/actions/*
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("enable", "action", "status"),
    [(True, "enable", "ACTIVE"), (False, "disable", "DISABLED")],
)
async def test_status_actions_use_numeric_path(panel_settings, enable, action, status):
    panel = RecordingPanelApiService(panel_settings, [ok(panel_user(status=status))])

    assert await panel.update_user_status_on_panel("42", enable) is True
    assert panel.last_call[:2] == ("POST", f"/users/42/actions/{action}")


async def test_status_action_reports_false_on_unexpected_status(panel_settings):
    panel = RecordingPanelApiService(panel_settings, [ok(panel_user(status="LIMITED"))])

    assert await panel.update_user_status_on_panel("42", True) is False


async def test_status_action_refuses_legacy_uuid(panel_settings):
    panel = RecordingPanelApiService(panel_settings, [ok(panel_user())])

    assert await panel.update_user_status_on_panel("2f9a-legacy", True) is False
    assert panel.calls == []


async def test_revoke_uses_numeric_path(panel_settings):
    panel = RecordingPanelApiService(panel_settings, [ok(panel_user(shortUuid="rotated"))])

    result = await panel.revoke_user_subscription("42", revoke_only_passwords=True)

    method, endpoint, kwargs = panel.last_call
    assert (method, endpoint) == ("POST", "/users/42/actions/revoke")
    assert kwargs["json"] == {"revokeOnlyPasswords": True}
    assert result["shortUuid"] == "rotated"


# --------------------------------------------------------------------------
# DELETE /api/users/{userId} — answers 204 with an empty body on 3.x
# --------------------------------------------------------------------------

async def test_delete_user_accepts_empty_204_body(panel_settings):
    panel = RecordingPanelApiService(panel_settings, [empty_body(204)])

    assert await panel.delete_user_from_panel("42") is True
    assert panel.last_call[:2] == ("DELETE", "/users/42")


@pytest.mark.parametrize("error_code", ["A062", "A025", "A063"])
async def test_delete_user_treats_missing_user_as_deleted(panel_settings, error_code):
    panel = RecordingPanelApiService(panel_settings, [api_error(404, error_code)])

    assert await panel.delete_user_from_panel("42") is True


async def test_delete_user_does_not_swallow_internal_errors(panel_settings):
    """A040 is INCREMENT_USED_TRAFFIC_ERROR (HTTP 500), never 'already gone'.

    It used to sit in the treat-as-deleted allowlist. Nothing was actually
    reported as deleted, because 5xx trips the transient guard first — but the
    allowlist entry was wrong, so pin the real behaviour here.
    """
    panel = RecordingPanelApiService(panel_settings, [api_error(500, "A040")])

    with pytest.raises(PanelUnavailableError):
        await panel.delete_user_from_panel("42")


async def test_delete_user_reports_failure_on_other_errors(panel_settings):
    panel = RecordingPanelApiService(panel_settings, [api_error(403, "A003")])

    assert await panel.delete_user_from_panel("42") is False


async def test_delete_user_with_legacy_uuid_is_a_noop(panel_settings):
    panel = RecordingPanelApiService(panel_settings, [empty_body(204)])

    assert await panel.delete_user_from_panel("2f9a-legacy") is True
    assert panel.calls == []


# --------------------------------------------------------------------------
# /api/hwid/devices — body switched from userUuid to userId in 3.0
# --------------------------------------------------------------------------

async def test_get_hwid_devices_uses_numeric_path(panel_settings):
    panel = RecordingPanelApiService(
        panel_settings, [ok({"total": 1, "devices": [{"hwid": "HW-1"}]})]
    )

    devices = await panel.get_user_hwid_devices("42")

    assert panel.last_call[:2] == ("GET", "/hwid/devices/42")
    assert devices == [{"hwid": "HW-1"}]


async def test_delete_one_hwid_device_sends_user_id(panel_settings):
    panel = RecordingPanelApiService(panel_settings, [ok({"total": 0, "devices": []})])

    assert await panel.delete_user_hwid_device("42", "HW-1") is True

    method, endpoint, kwargs = panel.last_call
    assert (method, endpoint) == ("POST", "/hwid/devices/delete")
    assert kwargs["json"] == {"userId": 42, "hwid": "HW-1"}
    assert "userUuid" not in kwargs["json"]


async def test_delete_all_hwid_devices_uses_bulk_route(panel_settings):
    panel = RecordingPanelApiService(
        panel_settings,
        [ok({"total": 3, "devices": [{"hwid": "a"}, {"hwid": "b"}, {"hwid": "c"}]})],
    )

    deleted = await panel.delete_all_user_hwid_devices("42")

    method, endpoint, kwargs = panel.last_call
    assert (method, endpoint) == ("POST", "/hwid/devices/delete-all")
    assert kwargs["json"] == {"userId": 42}
    assert deleted == 3
    assert len(panel.calls) == 1


async def test_delete_all_hwid_devices_falls_back_to_device_count(panel_settings):
    panel = RecordingPanelApiService(panel_settings, [ok({"devices": [{"hwid": "a"}]})])

    assert await panel.delete_all_user_hwid_devices("42") == 1


# --------------------------------------------------------------------------
# System routes and the removed happ crypt tool
# --------------------------------------------------------------------------

async def test_system_stats_route_unchanged(panel_settings):
    panel = RecordingPanelApiService(panel_settings, [ok({"cpu": 1})])

    await panel.get_system_stats()

    assert panel.last_call[:2] == ("GET", "/system/stats")


async def test_ping_raises_when_panel_is_down(panel_settings):
    panel = RecordingPanelApiService(panel_settings, [api_error(500)])

    with pytest.raises(PanelUnavailableError):
        await panel.ping()


async def test_happ_encrypt_returns_none_when_endpoint_removed(panel_settings, caplog):
    """The tool endpoint was dropped in panel 2.8.0; callers fall back to the raw link."""
    panel = RecordingPanelApiService(panel_settings, [api_error(404), api_error(404)])

    assert await panel.encrypt_happ_link("vless://x") is None
    assert await panel.encrypt_happ_link("vless://y") is None
    assert panel.last_call[:2] == ("POST", "/system/tools/happ/encrypt")


async def test_happ_encrypt_returns_link_when_supported(panel_settings):
    panel = RecordingPanelApiService(panel_settings, [ok({"encryptedLink": "happ://crypt4/zzz"})])

    assert await panel.encrypt_happ_link("vless://x") == "happ://crypt4/zzz"
