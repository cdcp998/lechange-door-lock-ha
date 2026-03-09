import logging
import time
import hashlib
import uuid
import json
import asyncio
from typing import Optional

import aiohttp
import async_timeout
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
import homeassistant.helpers.config_validation as cv
from .const import (
    DOMAIN,
    API_BASE,
    CONF_APP_ID,
    CONF_APP_SECRET,
    CONF_ACCESS_TOKEN,
    CONF_TOKEN_EXPIRE_TIME,
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
)

_LOGGER = logging.getLogger(__name__)



STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_APP_ID): cv.string,
        vol.Required(CONF_APP_SECRET): cv.string,
    }
)


def generate_sign(app_id: str, app_secret: str) -> dict:
    """Generate system parameters with sign."""
    time_utc = int(time.time())
    nonce = str(uuid.uuid4())
    sign_str = f"time:{time_utc},nonce:{nonce},appSecret:{app_secret}"
    sign = hashlib.md5(sign_str.encode()).hexdigest()
    return {
        "ver": "1.0",
        "appId": app_id,
        "time": time_utc,
        "nonce": nonce,
        "sign": sign,
    }


async def request_token(session, app_id: str, app_secret: str) -> Optional[dict]:
    """Request access token."""
    payload = {
        "system": generate_sign(app_id, app_secret),
        "id": str(uuid.uuid4()),
        "params": {},
    }
    url = f"{API_BASE}/accessToken"
    try:
        async with async_timeout.timeout(10):
            resp = await session.post(url, json=payload)
            text = await resp.text()
            try:
                data = json.loads(text)
            except Exception:
                _LOGGER.error("Invalid JSON: %s", text)
                return None
            result = data.get("result", {})
            if result.get("code") != "0":
                _LOGGER.error("Token error: %s", result.get("msg"))
                return None
            return result.get("data")
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        _LOGGER.error("Token request failed: %s", err)
        return None


async def list_devices(session, app_id: str, app_secret: str, token: str) -> Optional[list]:
    """Get all devices under account."""
    params = {"page": 1, "pageSize": 50, "source": "bindAndShare", "token": token}
    payload = {
        "system": generate_sign(app_id, app_secret),
        "id": str(uuid.uuid4()),
        "params": params,
    }
    url = f"{API_BASE}/listDeviceDetailsByPage"
    try:
        async with async_timeout.timeout(10):
            resp = await session.post(url, json=payload)
            text = await resp.text()
            data = json.loads(text)
            result = data.get("result", {})
            if result.get("code") != "0":
                _LOGGER.error("List devices error: %s", result.get("msg"))
                return None
            return result.get("data", {}).get("deviceList")
    except Exception as err:
        _LOGGER.error("List devices failed: %s", err)
        return None


async def is_lock_device(session, app_id: str, app_secret: str, token: str, device_id: str) -> bool:
    """Check if device is a lock by calling power info API."""
    params = {"token": token, "deviceId": device_id}
    payload = {
        "system": generate_sign(app_id, app_secret),
        "id": str(uuid.uuid4()),
        "params": params,
    }
    url = f"{API_BASE}/getDevicePowerInfo"
    try:
        async with async_timeout.timeout(10):
            resp = await session.post(url, json=payload)
            text = await resp.text()
            data = json.loads(text)
            result = data.get("result", {})
            if result.get("code") == "0" and "electricitys" in result.get("data", {}):
                return True
            return False
    except Exception:
        return False


async def validate_input(hass: HomeAssistant, data):
    """Validate user input and fetch devices."""
    app_id = data[CONF_APP_ID]
    app_secret = data[CONF_APP_SECRET]

    async with aiohttp.ClientSession() as session:
        # 1. Get access token
        token_data = await request_token(session, app_id, app_secret)
        if not token_data:
            raise ValueError("Failed to get access token")
        access_token = token_data["accessToken"]
        expire_time = int(time.time()) + token_data["expireTime"]

        # 2. List all devices
        all_devices = await list_devices(session, app_id, app_secret, access_token)
        if not all_devices:
            raise ValueError("No devices found")

        # 3. Filter lock devices (concurrently)
        tasks = [
            is_lock_device(session, app_id, app_secret, access_token, dev["deviceId"])
            for dev in all_devices
        ]
        results = await asyncio.gather(*tasks)
        lock_devices = [dev for dev, is_lock in zip(all_devices, results) if is_lock]

        if not lock_devices:
            raise ValueError("No lock devices found")

        return {
            "access_token": access_token,
            "expire_time": expire_time,
            "devices": lock_devices,
        }


class LeChangeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self):
        self._credentials = {}
        self._devices = []
        self._access_token = None
        self._token_expire_time = None

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            self._credentials = user_input
            try:
                result = await validate_input(self.hass, user_input)
                self._access_token = result["access_token"]
                self._token_expire_time = result["expire_time"]
                self._devices = result["devices"]
                if not self._devices:
                    errors["base"] = "no_devices"
                else:
                    return await self.async_step_device()
            except ValueError as e:
                _LOGGER.error("Validation error: %s", e)
                errors["base"] = "invalid_auth"
            except Exception as e:
                _LOGGER.exception("Unexpected exception: %s", e)
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_device(self, user_input=None):
        if user_input is not None:
            selected_device_id = user_input[CONF_DEVICE_ID]
            device = next(d for d in self._devices if d["deviceId"] == selected_device_id)
            await self.async_set_unique_id(selected_device_id)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=device.get("deviceName", selected_device_id),
                data={
                    CONF_APP_ID: self._credentials[CONF_APP_ID],
                    CONF_APP_SECRET: self._credentials[CONF_APP_SECRET],
                    CONF_ACCESS_TOKEN: self._access_token,
                    CONF_TOKEN_EXPIRE_TIME: self._token_expire_time,
                    CONF_DEVICE_ID: selected_device_id,
                    CONF_DEVICE_NAME: device.get("deviceName", ""),
                },
            )

        devices_dict = {
            d["deviceId"]: f"{d.get('deviceName', d['deviceId'])} ({d['deviceId']})"
            for d in self._devices
        }
        schema = vol.Schema({vol.Required(CONF_DEVICE_ID): vol.In(devices_dict)})
        return self.async_show_form(step_id="device", data_schema=schema)