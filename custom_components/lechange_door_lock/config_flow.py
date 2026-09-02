"""Config flow for the LeChange (Imou) door lock integration."""

from __future__ import annotations

import json
import logging
from typing import Optional

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
import homeassistant.helpers.config_validation as cv

from .const import (
    DOMAIN,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_SESSION_ID,
    CONF_TOKEN,
    CONF_INTERNAL_USERNAME,
    CONF_USER_ID,
    CONF_API_HOST,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_PRODUCT_ID,
    CONF_MODEL_NAME,
    CONF_FIRMWARE_VERSION,
    CONF_CHANNEL_JSON,
    CONF_LOCK_STATE,
    CONF_STREAM_ENTRY,
    CONF_RTSP_HOST,
    CONF_RTSP_PORT,
    CONF_RTSP_USERNAME,
    CONF_RTSP_PASSWORD,
    CONF_RTSP_URL,
    CONF_RTSP_SUBTYPE,
)
from .imou_client import ImouAPIError, ImouClient

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
    }
)


async def http_login(username: str, password: str) -> dict:
    """Login via the client-side API; returns session dict."""
    async with aiohttp.ClientSession() as session:
        client = ImouClient(session)
        data = await client.async_login(username, password)
        data["username_input"] = username
        data["password_input"] = password
        return data


async def http_list_devices(username: str, password: str, session_data: dict) -> list[dict]:
    """Login (reuse session) and list devices."""
    async with aiohttp.ClientSession() as session:
        client = ImouClient(
            session,
            username=username,
            password=password,
            session_id=session_data.get("session_id"),
            token=session_data.get("token"),
            internal_username=session_data.get("internal_username"),
            api_host=session_data.get("host"),
        )
        return await client.async_get_devices()


class LeChangeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Account + password -> device selection."""

    VERSION = 2

    def __init__(self) -> None:
        self._login_data: dict = {}
        self._devices: list[dict] = []

    async def async_step_user(self, user_input: Optional[dict] = None) -> FlowResult:
        errors = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            password = user_input[CONF_PASSWORD]
            try:
                login_data = await http_login(username, password)
                devices = await http_list_devices(username, password, login_data)
                devices.sort(key=lambda d: (not ImouClient.is_lock(d), d["name"]))
                if not devices:
                    raise ImouAPIError(-4, "no devices")
                self._login_data = login_data
                self._devices = devices
                return await self.async_step_device()
            except ImouAPIError as err:
                _LOGGER.error("Login failed: %s", err)
                if err.code == -4:
                    errors["base"] = "no_devices"
                elif err.code in (-2, -3):
                    errors["base"] = "network"
                else:
                    errors["base"] = "invalid_auth"
            except (aiohttp.ClientError, TimeoutError) as err:
                _LOGGER.error("Login network error: %s", err)
                errors["base"] = "network"
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Unexpected login error: %s", err)
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_device(self, user_input: Optional[dict] = None) -> FlowResult:
        if user_input is not None:
            device_id = user_input[CONF_DEVICE_ID]
            device = next(
                (d for d in self._devices if d["deviceId"] == device_id), None
            )
            if device is None:
                return self.async_abort(reason="device_not_found")
            await self.async_set_unique_id(device_id)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=device["name"],
                data={
                    CONF_USERNAME: self._login_data["username_input"],
                    CONF_PASSWORD: self._login_data["password_input"],
                    CONF_SESSION_ID: self._login_data["session_id"],
                    CONF_TOKEN: self._login_data["token"],
                    CONF_INTERNAL_USERNAME: self._login_data["internal_username"],
                    CONF_USER_ID: self._login_data.get("user_id"),
                    CONF_API_HOST: self._login_data["host"],
                    CONF_DEVICE_ID: device_id,
                    CONF_DEVICE_NAME: device["name"],
                    CONF_PRODUCT_ID: device["productId"],
                    CONF_MODEL_NAME: device["model"],
                    CONF_FIRMWARE_VERSION: device["version"],
                    CONF_CHANNEL_JSON: json.dumps(
                        device.get("channels", []), ensure_ascii=False
                    ),
                    CONF_LOCK_STATE: device.get("lockState", ""),
                    CONF_STREAM_ENTRY: device.get("stream_entry", ""),
                },
            )

        devices_dict = {
            d["deviceId"]: f"{d['name']} ({d['deviceId']}){'' if ImouClient.is_lock(d) else ' ·非门锁'}"
            for d in self._devices
        }
        schema = vol.Schema({vol.Required(CONF_DEVICE_ID): vol.In(devices_dict)})
        return self.async_show_form(step_id="device", data_schema=schema)

    @staticmethod
    def async_get_options_flow(config_entry):
        return LeChangeOptionsFlowHandler(config_entry)

    def async_migrate_entry(self, entry) -> bool:
        """Old OpenAPI-based entries cannot be migrated automatically."""
        return False


class LeChangeOptionsFlowHandler(config_entries.OptionsFlow):
    """Options flow: RTSP/video settings."""

    def __init__(self, config_entry) -> None:
        self._entry = config_entry

    async def async_step_init(self, user_input: Optional[dict] = None) -> FlowResult:
        data = self._entry.options or {}
        schema = vol.Schema(
            {
                vol.Optional(CONF_RTSP_URL, default=data.get(CONF_RTSP_URL, "")): cv.string,
                vol.Optional(CONF_RTSP_HOST, default=data.get(CONF_RTSP_HOST, "")): cv.string,
                vol.Optional(CONF_RTSP_PORT, default=int(data.get(CONF_RTSP_PORT, 554))): int,
                vol.Optional(
                    CONF_RTSP_USERNAME, default=data.get(CONF_RTSP_USERNAME, "admin")
                ): cv.string,
                vol.Optional(
                    CONF_RTSP_PASSWORD, default=data.get(CONF_RTSP_PASSWORD, "")
                ): cv.string,
                vol.Optional(
                    CONF_RTSP_SUBTYPE, default=int(data.get(CONF_RTSP_SUBTYPE, 0))
                ): vol.In([0, 1]),
            }
        )
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(step_id="init", data_schema=schema)
