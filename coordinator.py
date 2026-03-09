"""DataUpdateCoordinator for LeChange."""

import asyncio
import hashlib
import json
import logging
import time
import uuid
from datetime import timedelta
from typing import Optional, List

import aiohttp
import async_timeout

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers import device_registry as dr

from .const import (
    DOMAIN,
    DEFAULT_SCAN_INTERVAL,
    API_BASE,
    CONF_APP_ID,
    CONF_APP_SECRET,
    CONF_ACCESS_TOKEN,
    CONF_TOKEN_EXPIRE_TIME,
    CONF_DEVICE_ID,
)

_LOGGER = logging.getLogger(__name__)

TOKEN_REFRESH_MARGIN = 300


class LeChangeAPI:
    """LeChange Cloud API wrapper."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
    ):
        self.hass = hass
        self.entry = entry

        self.app_id = entry.data[CONF_APP_ID]
        self.app_secret = entry.data[CONF_APP_SECRET]

        self._access_token = entry.data.get(CONF_ACCESS_TOKEN)
        self._token_expire_time = entry.data.get(CONF_TOKEN_EXPIRE_TIME)

        self.device_id = entry.data[CONF_DEVICE_ID]

        self.session = async_get_clientsession(hass)

        self._token_lock = asyncio.Lock()

    async def _ensure_valid_token(self):
        """Ensure token is valid."""
        if not self._access_token or not self._token_expire_time:
            await self._refresh_token()
            return

        if self._token_expire_time - int(time.time()) < TOKEN_REFRESH_MARGIN:
            _LOGGER.debug("Token expiring soon, refreshing")
            await self._refresh_token()

    async def _refresh_token(self):
        """Refresh access token."""
        async with self._token_lock:
            token_data = await self._request_token()

            if not token_data:
                _LOGGER.error("Token refresh failed")
                return

            self._access_token = token_data["accessToken"]
            self._token_expire_time = int(time.time()) + token_data["expireTime"]

            _LOGGER.debug("Token refreshed")

            new_data = dict(self.entry.data)
            new_data[CONF_ACCESS_TOKEN] = self._access_token
            new_data[CONF_TOKEN_EXPIRE_TIME] = self._token_expire_time

            self.hass.config_entries.async_update_entry(self.entry, data=new_data)

    async def _request_token(self) -> Optional[dict]:
        """Request access token."""
        payload = {
            "system": self._generate_sign(),
            "id": str(uuid.uuid4()),
            "params": {},
        }

        url = f"{API_BASE}/accessToken"

        try:
            async with async_timeout.timeout(10):
                resp = await self.session.post(url, json=payload)

                if resp.status != 200:
                    _LOGGER.error("Token HTTP error: %s", resp.status)
                    return None

                text = await resp.text()
                data = self._parse_json(text)

                if not data:
                    return None

                result = data.get("result", {})

                if result.get("code") != "0":
                    _LOGGER.error("Token error: %s", result.get("msg"))
                    return None

                return result.get("data")

        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.error("Token request failed: %s", err)
            return None

    def _generate_sign(self) -> dict:
        """Generate request signature."""
        time_utc = int(time.time())
        nonce = str(uuid.uuid4())

        sign_str = f"time:{time_utc},nonce:{nonce},appSecret:{self.app_secret}"
        sign = hashlib.md5(sign_str.encode()).hexdigest()

        return {
            "ver": "1.0",
            "appId": self.app_id,
            "time": time_utc,
            "nonce": nonce,
            "sign": sign,
        }

    def _parse_json(self, text: str) -> Optional[dict]:
        """Parse JSON safely."""
        try:
            return json.loads(text)
        except Exception:
            _LOGGER.error("Invalid JSON response: %s", text)
            return None

    async def _request(self, method: str, params: dict = None) -> Optional[dict]:
        """Send API request."""
        await self._ensure_valid_token()

        if params is None:
            params = {}

        params["token"] = self._access_token
        params["deviceId"] = self.device_id

        payload = {
            "system": self._generate_sign(),
            "id": str(uuid.uuid4()),
            "params": params,
        }

        url = f"{API_BASE}/{method}"

        try:
            async with async_timeout.timeout(10):
                resp = await self.session.post(url, json=payload)

                if resp.status != 200:
                    _LOGGER.error("HTTP error %s", resp.status)
                    return None

                text = await resp.text()
                data = self._parse_json(text)

                if not data:
                    return None

                result = data.get("result", {})
                code = result.get("code")

                if code == "0":
                    return result.get("data")

                if code == "TK1002":
                    _LOGGER.warning("Token expired, refreshing")
                    await self._refresh_token()
                    return await self._request(method, params)

                _LOGGER.error("API error %s: %s", code, result.get("msg"))
                return None

        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.error("API request failed: %s", err)
            return None

    async def async_device_online(self):
        return await self._request("deviceOnline")

    async def async_get_device_power_info(self):
        return await self._request("getDevicePowerInfo")

    async def async_get_device_details(self):
        data = await self._request(
            "listDeviceDetailsByPage",
            {"page": 1, "pageSize": 10, "source": "bindAndShare"},
        )

        if data and "deviceList" in data:
            for device in data["deviceList"]:
                if device["deviceId"] == self.device_id:
                    return device

        return None

    async def async_open_door_remote(self):
        return await self._request("openDoorRemote")

    async def async_wake_up_device(self):
        return await self._request("wakeUpDevice", {"url": "/device/wakeup"})

    async def async_generate_snapkey(
        self, name, effective_num, effective_day, effect_period, begin_time, end_time
    ):
        return await self._request(
            "generateSnapkey",
            {
                "name": name,
                "effectiveNum": effective_num,
                "effectiveDay": effective_day,
                "effectPeriod": effect_period,
                "beginTime": begin_time,
                "endTime": end_time,
            },
        )

    async def async_get_snapkey_list(self):
        return await self._request("getSnapkeyList")


class LeChangeDataUpdateCoordinator(DataUpdateCoordinator):
    """Coordinator for LeChange device."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self.entry = entry
        self.device_id = entry.data[CONF_DEVICE_ID]

        self.api = LeChangeAPI(hass, entry)

        self._version_update_unsub = None

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{self.device_id}",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )

        self._version_update_unsub = async_track_time_interval(
            hass,
            self._async_update_device_info,
            timedelta(days=1),
        )

    async def _async_update_data(self):
        """Fetch device status."""
        data = {}

        online_result = await self.api.async_device_online()

        if online_result:
            data["online"] = online_result.get("onLine")
            data["channels"] = online_result.get("channels", [])

        else:
            data["online"] = None
            return data

        if data["online"] == "4":
            power_result = await self.api.async_get_device_power_info()

            if power_result and "electricitys" in power_result:
                battery = power_result["electricitys"][0]
                data["battery_level"] = battery.get("electric")
                data["battery_type"] = battery.get("type")

        _LOGGER.debug("Coordinator data: %s", data)

        return data

    async def _async_update_device_info(self, now=None):
        """Update firmware and model info."""
        details = await self.api.async_get_device_details()

        if not details:
            return

        version = details.get("deviceVersion")
        model = details.get("deviceModel")

        device_registry = dr.async_get(self.hass)

        device_entry = device_registry.async_get_device(
            identifiers={(DOMAIN, self.device_id)}
        )

        if not device_entry:
            return

        updates = {}

        if version:
            updates["sw_version"] = version

        if model:
            updates["model"] = model

        if updates:
            device_registry.async_update_device(device_entry.id, **updates)

    async def async_update_device_info(self):
        """Public method to trigger device info update."""
        await self._async_update_device_info()

    async def async_shutdown(self):
        """Cleanup."""
        if self._version_update_unsub:
            self._version_update_unsub()
            self._version_update_unsub = None
