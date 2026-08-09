import aiohttp
import logging
import json
import re
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta, timezone
import asyncio
from urllib.parse import urlencode

from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import Settings
from db.dal import panel_sync_dal
from db.models import PanelSyncStatus


class PanelUnavailableError(Exception):
    """Raised when the Remnawave panel is unreachable or returned a 5xx.

    Distinct from logical errors (user not found, validation failure): callers
    MUST NOT interpret this as "entity does not exist" and MUST NOT mutate
    local state based on it. Propagate to the user as "technical works".
    """


def extract_panel_user_ref(panel_user: Optional[Dict[str, Any]]) -> Optional[str]:
    """Return the panel's identifier for a user object as a string.

    Remnawave 3.0 dropped `User.uuid` and identifies users by a numeric `id`.
    Panels <=2.8 still answer with `uuid`. Reading both keeps the bot working
    across an in-flight panel upgrade; everything downstream (DB column,
    service args) treats the value as an opaque string.
    """
    if not isinstance(panel_user, dict):
        return None
    panel_id = panel_user.get("id")
    if panel_id is not None:
        return str(panel_id)
    legacy_uuid = panel_user.get("uuid")
    return str(legacy_uuid) if legacy_uuid else None


def panel_user_id(user_ref: Optional[str]) -> Optional[int]:
    """Coerce a stored panel reference to the numeric id the 3.x API expects.

    Returns None for legacy UUID references, which the caller must re-resolve
    (by telegramId or username) instead of sending to the panel.
    """
    if user_ref is None:
        return None
    try:
        return int(str(user_ref).strip())
    except (TypeError, ValueError):
        return None


def panel_username_for_telegram_id(telegram_id: int) -> str:
    """The deterministic panel username this bot provisions users under."""
    return f"tg_{telegram_id}"


# Panel error codes that mean "this user does not exist" (all 404s) rather than
# "the request failed". Verified against libs/contract/constants/errors in 3.2.1:
# A025 USER_NOT_FOUND, A062 USERS_NOT_FOUND,
# A063 GET_USER_BY_UNIQUE_FIELDS_NOT_FOUND (returned by by-username/by-short-uuid).
USER_ABSENT_ERROR_CODES = frozenset({"A025", "A062", "A063"})


class PanelApiService:

    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_url = settings.PANEL_API_URL
        self.api_key = settings.PANEL_API_KEY
        self._session: Optional[aiohttp.ClientSession] = None
        self.default_client_ip = "127.0.0.1"
        self._happ_encrypt_unsupported_logged = False

    async def __aenter__(self):
        """Context manager entry"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - automatically close session"""
        await self.close_session()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=10)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    @staticmethod
    def _is_transient_error(response: Optional[Dict[str, Any]]) -> bool:
        """Detect transport-level failures and 5xx responses. Logical errors
        (400/401/403/404/A062) are NOT transient — they reflect real state."""
        if not response or not response.get("error"):
            return False
        status = response.get("status_code")
        if status is None:
            return False
        try:
            status_int = int(status)
        except (TypeError, ValueError):
            return False
        if status_int < 0:
            return True
        if 500 <= status_int < 600:
            return True
        return False

    def _raise_if_transient(self, response: Optional[Dict[str, Any]], context: str) -> None:
        if self._is_transient_error(response):
            message = (
                response.get("message")
                or (response.get("details") or {}).get("message")
                or "panel unreachable"
            ) if response else "panel unreachable"
            raise PanelUnavailableError(f"{context}: {message}")

    async def close_session(self):
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
            logging.debug("Panel API service HTTP session closed.")

    async def close(self):
        """Alias for close_session for API consistency."""
        await self.close_session()

    async def _prepare_headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-For": self.default_client_ip,
            "X-Real-IP": self.default_client_ip,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    @staticmethod
    def _sanitize_payload_for_log(payload: Any) -> Any:
        if isinstance(payload, dict):
            redacted: Dict[str, Any] = {}
            for key, value in payload.items():
                lowered = str(key).lower()
                if any(mask_key in lowered for mask_key in (
                    "token",
                    "secret",
                    "password",
                    "authorization",
                    "api_key",
                    "apikey",
                    "key",
                )):
                    redacted[key] = "***"
                else:
                    redacted[key] = PanelApiService._sanitize_payload_for_log(value)
            return redacted
        if isinstance(payload, list):
            return [PanelApiService._sanitize_payload_for_log(item) for item in payload]
        return payload

    async def _request(self,
                       method: str,
                       endpoint: str,
                       log_full_response: bool = False,
                       **kwargs) -> Optional[Dict[str, Any]]:
        if not self.base_url:
            logging.error(
                "Panel API URL (PANEL_API_URL) not configured in settings.")
            return {
                "error": True,
                "status_code": 0,
                "message": "Panel API URL not configured."
            }

        aiohttp_session = await self._get_session()
        headers = await self._prepare_headers()

        url_for_request = f"{self.base_url.rstrip('/')}/{endpoint.lstrip('/')}"

        current_params = kwargs.get("params")
        url_with_params_for_log = url_for_request
        if current_params:
            try:
                url_with_params_for_log += "?" + urlencode(current_params)
            except Exception as exc:
                logging.debug("Failed to encode params for panel API log URL: %s", exc)

        json_payload_for_log = kwargs.get('json') if method.upper() in [
            "POST", "PATCH", "PUT"
        ] else None
        log_prefix = f"Panel API Req: {method.upper()} {url_with_params_for_log}"
        if json_payload_for_log:
            try:
                sanitized_payload = self._sanitize_payload_for_log(json_payload_for_log)
                payload_str = json.dumps(sanitized_payload)
                log_prefix += f" | Payload: {payload_str[:300]}{'...' if len(payload_str) > 300 else ''}"
            except Exception:
                log_prefix += " | Payload: <unavailable>"
        try:
            async with aiohttp_session.request(method.upper(),
                                               url_for_request,
                                               headers=headers,
                                               **kwargs) as response:
                response_status = response.status
                response_text = await response.text()

                log_suffix = f"| Status: {response_status}"

                should_log_full_body = bool(log_full_response and self.settings.LOG_LEVEL == "DEBUG")
                if should_log_full_body or not (200 <= response_status < 300):
                    try:
                        parsed_json_for_log = json.loads(response_text)
                        pretty_response_text = json.dumps(parsed_json_for_log,
                                                          indent=2,
                                                          ensure_ascii=False)
                        logging.info(
                            f"{log_prefix} {log_suffix} | Full Response Body:\n{pretty_response_text}"
                        )
                    except json.JSONDecodeError:
                        logging.info(
                            f"{log_prefix} {log_suffix} | Full Response Text (not JSON):\n{response_text[:2000]}{'...' if len(response_text) > 2000 else ''}"
                        )
                else:
                    logging.debug(
                        f"{log_prefix} {log_suffix} | OK. Response Body Preview: {response_text[:200]}{'...' if len(response_text) > 200 else ''}"
                    )

                if 200 <= response_status < 300:
                    # Remnawave 3.x answers 204/202 with an empty body for every
                    # DELETE, async bulk action and node restart. Normalise those
                    # into a success envelope so callers can keep checking for
                    # "response" / "error" uniformly.
                    if response_status in (202, 204) or not response_text.strip():
                        return {
                            "status": "success",
                            "code": response_status,
                            "response": None,
                            "empty_body": True,
                        }
                    try:
                        if 'application/json' in response.headers.get(
                                'Content-Type', '').lower():
                            data = json.loads(response_text)
                            return data
                        else:
                            return {
                                "status": "success",
                                "code": response_status,
                                "data_text": response_text
                            }
                    except json.JSONDecodeError as e_json_ok:
                        logging.error(
                            f"{log_prefix} {log_suffix} | OK but JSON Parse Error. Error: {e_json_ok}. Body was logged above."
                        )
                        return {
                            "status": "success_parse_error",
                            "code": response_status,
                            "data_text": response_text,
                            "parse_error": str(e_json_ok)
                        }
                else:
                    error_details = {
                        "message":
                        f"Request failed with status {response_status}",
                        "raw_response_text": response_text
                    }
                    try:
                        if 'application/json' in response.headers.get(
                                'Content-Type', '').lower():
                            error_json_data = json.loads(response_text)
                            error_details.update(error_json_data)
                    except json.JSONDecodeError:
                        pass
                    return {
                        "error": True,
                        "status_code": response_status,
                        "details": error_details
                    }

        except aiohttp.ClientConnectorError as e:
            logging.error(
                f"Panel API ClientConnectorError to {url_for_request}: {e}")
            return {
                "error": True,
                "status_code": -1,
                "message": f"Connection error: {str(e)}"
            }
        except aiohttp.ClientError as e:
            logging.error(f"Panel API ClientError to {url_for_request}: {e}")
            return {
                "error": True,
                "status_code": -2,
                "message": f"Client error: {str(e)}"
            }
        except asyncio.TimeoutError:
            logging.error(f"Panel API request to {url_for_request} timed out.")
            return {
                "error": True,
                "status_code": -3,
                "message": "Request timed out"
            }
        except Exception as e:
            logging.error(
                f"Unexpected Panel API request error to {url_for_request}: {e}",
                exc_info=True)
            return {
                "error": True,
                "status_code": -4,
                "message": f"Unexpected error: {str(e)}"
            }

    @staticmethod
    def _error_code(response: Optional[Dict[str, Any]]) -> Optional[str]:
        """Panel error code, whether it arrives at the top level or under details."""
        if not isinstance(response, dict):
            return None
        details = response.get("details")
        if isinstance(details, dict) and details.get("errorCode"):
            return details["errorCode"]
        return response.get("errorCode")

    @classmethod
    def _is_absent(cls, response: Optional[Dict[str, Any]]) -> bool:
        """True when the panel answered 'no such user' rather than failing.

        A025 USER_NOT_FOUND — /users/{id}, DELETE, actions.
        A062 USERS_NOT_FOUND — collection lookups.
        A063 GET_USER_BY_UNIQUE_FIELDS_NOT_FOUND — by-username / by-short-uuid.
        All three are 404s; anything else (including 5xx) is a real failure.
        """
        if cls._error_code(response) in USER_ABSENT_ERROR_CODES:
            return True
        return isinstance(response, dict) and response.get("status_code") == 404

    async def _stream_users(
            self,
            log_responses: bool = False,
            page_size: int = 500,
            **filters: Any) -> Optional[List[Dict[str, Any]]]:
        """Cursor-paginated GET /api/users/stream.

        Replaces the offset pagination and the removed /users/by-telegram-id and
        /users/by-email lookups. `telegramId`, `email`, `tag`, `status` and
        `externalSquadUuid` are first-class indexed query filters here, unlike
        the LIKE-based filters of GET /api/users.
        """
        all_users: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        while True:
            params: Dict[str, Any] = {"size": page_size}
            params.update({k: v for k, v in filters.items() if v is not None})
            if cursor is not None:
                params["cursor"] = cursor

            response_data = await self._request(
                "GET",
                "/users/stream",
                params=params,
                log_full_response=log_responses)
            self._raise_if_transient(response_data, "stream_users")

            if not response_data or response_data.get("error"):
                if self._error_code(response_data) == "A062":
                    return all_users
                logging.error(
                    f"Failed to fetch panel users batch (cursor: {cursor}). Response: {response_data}"
                )
                return None

            payload = response_data.get("response") or {}
            users_batch = payload.get("users") or []
            all_users.extend(users_batch)

            cursor = payload.get("nextCursor")
            if not payload.get("hasMore") or not cursor:
                break
            await asyncio.sleep(0.1)
        return all_users

    async def get_all_panel_users(
            self,
            page_size: int = 500,
            log_responses: bool = False) -> Optional[List[Dict[str, Any]]]:
        all_users = await self._stream_users(
            log_responses=log_responses, page_size=page_size)
        if all_users is None:
            return None
        logging.info(f"Fetched {len(all_users)} users from panel API.")
        return all_users

    async def get_user_by_ref(
            self,
            user_ref: str,
            log_response: bool = True) -> Optional[Dict[str, Any]]:
        """GET /api/users/{userId}. `user_ref` is the panel's numeric user id."""
        numeric_id = panel_user_id(user_ref)
        if numeric_id is None:
            logging.warning(
                "Panel reference '%s' is not a numeric id (legacy UUID from a pre-3.0 "
                "panel). Skipping direct lookup; caller must re-resolve by telegramId "
                "or username.", user_ref)
            return None

        endpoint = f"/users/{numeric_id}"
        full_response = await self._request("GET",
                                            endpoint,
                                            log_full_response=log_response)
        self._raise_if_transient(full_response, f"get_user_by_ref({user_ref})")
        if full_response and not full_response.get(
                "error") and "response" in full_response:
            return full_response.get("response")

        return None

    async def get_user(
        self,
        *,
        user_ref: Optional[str] = None,
        telegram_id: Optional[int] = None,
        username: Optional[str] = None,
        email: Optional[str] = None,
        log_response: bool = True,
    ) -> Optional[Dict[str, Any]]:
        if user_ref:
            return await self.get_user_by_ref(user_ref, log_response=log_response)

        users = await self.get_users_by_filter(
            telegram_id=telegram_id,
            username=username,
            email=email,
            log_response=log_response,
        )
        if users:
            return users[0]
        return None

    async def get_users_by_filter(
            self,
            telegram_id: Optional[int] = None,
            username: Optional[str] = None,
            email: Optional[str] = None,
            log_response: bool = True) -> Optional[List[Dict[str, Any]]]:

        if telegram_id is not None:
            return await self._stream_users(
                log_responses=log_response, telegramId=int(telegram_id))

        if email is not None:
            return await self._stream_users(
                log_responses=log_response, email=email)

        if username is not None:
            filter_used_log = f"username={username}"
            endpoint = f"/users/by-username/{username}"
            response_data = await self._request("GET",
                                                endpoint,
                                                log_full_response=log_response)
            self._raise_if_transient(response_data, f"get_users_by_filter({filter_used_log})")

            if response_data and not response_data.get(
                    "error") and isinstance(response_data.get("response"), dict):
                return [response_data["response"]]
            if self._is_absent(response_data):
                logging.info(
                    f"Panel API: User not found for {filter_used_log}")
                return []

            logging.error(
                f"Failed to fetch panel users with filter ({filter_used_log}). "
                f"Last API response: {response_data if not log_response else '(logged above)'}"
            )
            return None

        logging.warning(
            "get_users_by_filter called without any specific filter criteria.")
        return []

    async def create_panel_user(
            self,
            username_on_panel: str,
            telegram_id: Optional[int] = None,
            email: Optional[str] = None,
            default_expire_days: int = 1,
            default_traffic_limit_bytes: int = 0,
            default_traffic_limit_strategy: str = "NO_RESET",
            hwid_device_limit: Optional[int] = None,
            specific_squad_uuids: Optional[List[str]] = None,
            external_squad_uuid: Optional[str] = None,
            description: Optional[str] = None,
            tag: Optional[str] = None,
            status: str = "ACTIVE",
            log_response: bool = True) -> Optional[Dict[str, Any]]:

        username_is_valid = (
            3 <= len(username_on_panel) <= 36
            and re.match(r"^[A-Za-z0-9_-]+$", username_on_panel) is not None
        )
        if not username_is_valid:
            msg = f"Panel username '{username_on_panel}' does not meet panel requirements."
            logging.error(msg)
            return {
                "error": True,
                "status_code": 400,
                "message": msg,
                "errorCode": "VALIDATION_ERROR_USERNAME"
            }

        now = datetime.now(timezone.utc)
        expire_at_dt = now + timedelta(days=default_expire_days)
        expire_at_iso = expire_at_dt.isoformat(
            timespec='milliseconds').replace('+00:00', 'Z')

        payload: Dict[str, Any] = {
            "username": username_on_panel,
            "status": status.upper(),
            "expireAt": expire_at_iso,
            "trafficLimitStrategy": default_traffic_limit_strategy.upper(),
            "trafficLimitBytes": default_traffic_limit_bytes,
        }
        hwid_limit_value = hwid_device_limit
        if hwid_limit_value is None:
            hwid_limit_value = self.settings.USER_HWID_DEVICE_LIMIT
        if hwid_limit_value is not None:
            try:
                hwid_limit_int = int(hwid_limit_value)
                if hwid_limit_int >= 0:
                    payload["hwidDeviceLimit"] = hwid_limit_int
            except (TypeError, ValueError):
                logging.warning(
                    f"Ignoring invalid HWID device limit '{hwid_limit_value}' while creating panel user '{username_on_panel}'."
                )
        if specific_squad_uuids:
            payload["activeInternalSquads"] = specific_squad_uuids
        if external_squad_uuid:
            payload["externalSquadUuid"] = external_squad_uuid
        if telegram_id is not None: payload["telegramId"] = telegram_id
        if email: payload["email"] = email
        if description: payload["description"] = description
        if tag: payload["tag"] = tag

        response = await self._request("POST",
                                       "/users",
                                       json=payload,
                                       log_full_response=log_response)
        self._raise_if_transient(response, f"create_panel_user({username_on_panel})")
        if response and not response.get("error") and "response" in response:
            logging.info(
                f"Panel user '{username_on_panel}' created successfully "
                f"(panel id: {extract_panel_user_ref(response.get('response'))})."
            )
            return response

        logging.error(
            "Failed to create panel user '%s'. Payload: %s, Response: %s",
            username_on_panel,
            self._sanitize_payload_for_log(payload),
            response if not log_response else "(full response logged above)",
        )
        return response

    async def update_user_details_on_panel(
            self,
            user_ref: str,
            update_payload: Dict[str, Any],
            log_response: bool = True) -> Optional[Dict[str, Any]]:
        """PATCH /api/users. The body identifies the user by numeric `id`
        (3.x) — `uuid` is no longer accepted."""
        numeric_id = panel_user_id(user_ref)
        if numeric_id is None:
            logging.error(
                "Cannot update panel user: '%s' is not a numeric panel id. "
                "Re-resolve the user by telegramId or username first.", user_ref)
            return None

        payload = dict(update_payload)
        payload.pop("uuid", None)
        payload["id"] = numeric_id

        full_response = await self._request("PATCH",
                                            "/users",
                                            json=payload,
                                            log_full_response=log_response)
        self._raise_if_transient(full_response, f"update_user_details_on_panel({user_ref})")
        if full_response and not full_response.get(
                "error") and "response" in full_response:
            logging.info(f"User {user_ref} details updated on panel.")
            return full_response.get("response")

        logging.error(
            "Failed to update user %s details on panel. Payload: %s, Response: %s",
            user_ref,
            self._sanitize_payload_for_log(payload),
            full_response if not log_response else "(logged above)",
        )
        return None

    async def update_user_status_on_panel(self,
                                          user_ref: str,
                                          enable: bool,
                                          log_response: bool = True) -> bool:
        action = "enable" if enable else "disable"
        numeric_id = panel_user_id(user_ref)
        if numeric_id is None:
            logging.error(
                "Cannot %s panel user: '%s' is not a numeric panel id.", action, user_ref)
            return False
        endpoint = f"/users/{numeric_id}/actions/{action}"
        response_data = await self._request("POST",
                                            endpoint,
                                            log_full_response=log_response)
        self._raise_if_transient(response_data, f"update_user_status_on_panel({user_ref}, {action})")

        if response_data and not response_data.get(
                "error") and "response" in response_data:
            actual_status = (response_data.get("response") or {}).get("status")
            expected_status = "ACTIVE" if enable else "DISABLED"
            if actual_status == expected_status:
                logging.info(
                    f"User {user_ref} status on panel successfully set to {action} (Actual: {actual_status})."
                )
                return True
            else:
                logging.warning(
                    f"User {user_ref} status on panel action '{action}' called, but final status is '{actual_status}'."
                )
                return False

        logging.error(
            f"Failed to {action} user {user_ref} on panel. Response: {response_data if not log_response else '(logged above)'}"
        )
        return False

    async def delete_user_from_panel(self,
                                     user_ref: str,
                                     log_response: bool = True) -> bool:
        """DELETE /api/users/{userId}. Answers 204 with an empty body on 3.x.
        Treat not-found as already deleted."""
        numeric_id = panel_user_id(user_ref)
        if numeric_id is None:
            logging.warning(
                "Cannot delete panel user: '%s' is not a numeric panel id. "
                "Treating as already absent.", user_ref)
            return True

        endpoint = f"/users/{numeric_id}"
        response_data = await self._request(
            "DELETE", endpoint, log_full_response=log_response
        )
        self._raise_if_transient(response_data, f"delete_user_from_panel({user_ref})")

        if not response_data:
            logging.error(
                f"Panel API delete_user_from_panel returned no data for user {user_ref}."
            )
            return False

        if response_data.get("error"):
            if self._is_absent(response_data):
                logging.info(
                    f"Panel user {user_ref} already absent "
                    f"(errorCode {self._error_code(response_data)}). Treating as deleted."
                )
                return True
            logging.error(
                f"Failed to delete user {user_ref} on panel. Response: {response_data}"
            )
            return False

        logging.info(f"Panel user {user_ref} deleted successfully.")
        return True

    async def revoke_user_subscription(
            self,
            user_ref: str,
            revoke_only_passwords: bool = False,
            log_response: bool = True) -> Optional[Dict[str, Any]]:
        """Revoke user's subscription — panel generates a new shortUuid/subscriptionUrl
        and rotates credentials, invalidating the old link on all devices. Dates and
        traffic limits are preserved."""
        numeric_id = panel_user_id(user_ref)
        if numeric_id is None:
            logging.error(
                "Cannot revoke subscription: '%s' is not a numeric panel id.", user_ref)
            return None

        endpoint = f"/users/{numeric_id}/actions/revoke"
        payload: Dict[str, Any] = {}
        if revoke_only_passwords:
            payload["revokeOnlyPasswords"] = True
        response_data = await self._request(
            "POST",
            endpoint,
            json=payload,
            log_full_response=log_response,
        )
        self._raise_if_transient(response_data, f"revoke_user_subscription({user_ref})")
        if response_data and not response_data.get("error") and "response" in response_data:
            logging.info(f"Panel user {user_ref} subscription revoked successfully.")
            return response_data.get("response")
        logging.error(
            f"Failed to revoke subscription for user {user_ref}. Response: {response_data if not log_response else '(logged above)'}"
        )
        return None

    async def get_user_hwid_devices(self, user_ref: str) -> Optional[List[Dict[str, Any]]]:
        """GET /api/hwid/devices/{userId}."""
        numeric_id = panel_user_id(user_ref)
        if numeric_id is None:
            logging.error(
                "Cannot list HWID devices: '%s' is not a numeric panel id.", user_ref)
            return None

        endpoint = f"/hwid/devices/{numeric_id}"
        response_data = await self._request("GET", endpoint)
        self._raise_if_transient(response_data, f"get_user_hwid_devices({user_ref})")
        if response_data and not response_data.get("error") and "response" in response_data:
            payload = response_data.get("response") or {}
            devices = payload.get("devices")
            if isinstance(devices, list):
                return devices
            return []
        logging.error(
            f"Failed to fetch HWID devices for user {user_ref}. Response: {response_data}"
        )
        return None

    async def delete_user_hwid_device(self, user_ref: str, hwid: str) -> bool:
        """POST /api/hwid/devices/delete — body takes `userId` since 3.0."""
        numeric_id = panel_user_id(user_ref)
        if numeric_id is None:
            logging.error(
                "Cannot delete HWID device: '%s' is not a numeric panel id.", user_ref)
            return False

        endpoint = "/hwid/devices/delete"
        payload = {"userId": numeric_id, "hwid": hwid}
        response_data = await self._request("POST", endpoint, json=payload)
        self._raise_if_transient(
            response_data, f"delete_user_hwid_device({user_ref}, {hwid})"
        )
        if response_data and not response_data.get("error"):
            return True
        logging.error(
            f"Failed to delete HWID device {hwid} for user {user_ref}. Response: {response_data}"
        )
        return False

    async def delete_all_user_hwid_devices(self, user_ref: str) -> int:
        """POST /api/hwid/devices/delete-all — one call replaces the per-device loop."""
        numeric_id = panel_user_id(user_ref)
        if numeric_id is None:
            logging.error(
                "Cannot delete HWID devices: '%s' is not a numeric panel id.", user_ref)
            return 0

        response_data = await self._request(
            "POST", "/hwid/devices/delete-all", json={"userId": numeric_id}
        )
        self._raise_if_transient(
            response_data, f"delete_all_user_hwid_devices({user_ref})"
        )
        if response_data and not response_data.get("error"):
            payload = response_data.get("response") or {}
            deleted = payload.get("total")
            if deleted is None:
                deleted = len(payload.get("devices") or [])
            logging.info(
                f"Deleted {deleted} HWID devices for user {user_ref}."
            )
            return int(deleted)

        logging.error(
            f"Failed to delete all HWID devices for user {user_ref}. Response: {response_data}"
        )
        return 0

    async def get_subscription_link(
            self,
            short_uuid_or_sub_uuid: str,
            client_type: Optional[str] = None) -> Optional[str]:
        if not self.settings.PANEL_API_URL:
            logging.error(
                "PANEL_API_URL not set, cannot generate subscription link.")
            return None
        base_sub_url = f"{self.settings.PANEL_API_URL.rstrip('/')}/sub/{short_uuid_or_sub_uuid}"
        if client_type:
            return f"{base_sub_url}/{client_type.lower()}"
        return base_sub_url

    async def get_user_devices(self, user_ref: str) -> Optional[List[Dict[str, Any]]]:
        """GET /api/hwid/devices/{userId} — list of user's HWID devices."""
        return await self.get_user_hwid_devices(user_ref)

    async def disconnect_device(self, user_ref: str, hwid: str) -> bool:
        """POST /api/hwid/devices/delete — remove one device."""
        return await self.delete_user_hwid_device(user_ref, hwid)

    async def update_bot_db_sync_status(self,
                                        session: AsyncSession,
                                        status: str,
                                        details: str,
                                        users_processed: int = 0,
                                        subs_synced: int = 0):
        await panel_sync_dal.update_panel_sync_status(session, status, details,
                                                      users_processed,
                                                      subs_synced)

    async def get_bot_db_last_sync_status(
            self, session: AsyncSession) -> Optional[PanelSyncStatus]:
        return await panel_sync_dal.get_panel_sync_status(session)

    async def get_system_stats(self) -> Optional[Dict[str, Any]]:
        """Get system statistics (CPU, memory, users counts)"""
        response_data = await self._request("GET", "/system/stats", log_full_response=False)
        if response_data and not response_data.get("error") and "response" in response_data:
            return response_data.get("response")
        return None

    async def ping(self) -> None:
        """Lightweight reachability check. Raises PanelUnavailableError on
        transport/5xx failure. Returns None on success. Used to gate flows
        (like the Buy screen) that must not start when the panel is down."""
        response_data = await self._request("GET", "/system/stats", log_full_response=False)
        self._raise_if_transient(response_data, "ping()")

    async def get_bandwidth_stats(self) -> Optional[Dict[str, Any]]:
        """Get bandwidth statistics"""
        response_data = await self._request("GET", "/system/stats/bandwidth", log_full_response=False)
        if response_data and not response_data.get("error") and "response" in response_data:
            return response_data.get("response")
        return None

    async def get_nodes_statistics(self) -> Optional[Dict[str, Any]]:
        """Get nodes statistics"""
        response_data = await self._request("GET", "/system/stats/nodes", log_full_response=False)
        if response_data and not response_data.get("error") and "response" in response_data:
            return response_data.get("response")
        return None

    async def get_nodes(self) -> Optional[List[Dict[str, Any]]]:
        """Get list of all nodes with their current connection status"""
        response_data = await self._request("GET", "/nodes", log_full_response=False)
        if response_data and not response_data.get("error") and "response" in response_data:
            resp = response_data.get("response")
            if isinstance(resp, list):
                return resp
        return None

    async def encrypt_happ_link(self, link_to_encrypt: str) -> Optional[str]:
        """Encrypt a subscription link using the panel's happ crypt4 API.

        NOTE: POST /api/system/tools/happ/encrypt was removed from the panel in
        2.8.0 and does not exist on 3.x. On such panels this returns None and the
        caller falls back to the raw subscription link. Turn CRYPT4_ENABLED off.

        Returns the encrypted link string or None if encryption failed.
        """
        payload = {"linkToEncrypt": link_to_encrypt}
        response_data = await self._request(
            "POST",
            "/system/tools/happ/encrypt",
            json=payload,
            log_full_response=False
        )
        if response_data and not response_data.get("error") and "response" in response_data:
            return (response_data.get("response") or {}).get("encryptedLink")

        if response_data and response_data.get("status_code") in (404, 400):
            if not self._happ_encrypt_unsupported_logged:
                self._happ_encrypt_unsupported_logged = True
                logging.error(
                    "CRYPT4_ENABLED is on, but this panel has no "
                    "/api/system/tools/happ/encrypt endpoint (removed in Remnawave 2.8.0). "
                    "Set CRYPT4_ENABLED=false — raw subscription links are used meanwhile."
                )
            return None

        logging.error(f"Failed to encrypt happ link. Response: {response_data}")
        return None
