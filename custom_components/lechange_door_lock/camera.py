"""Camera platform (视频): doorbell channels with cloud gateway default.

The lock's camera channels (主摄像头/辅摄像头) are reachable through the
cloud stream gateway that the account provides (streamEntryAddrV3 /
mediaConfig.streamUrl, e.g. nginxdeviceproxy-online-hz.imou.com:443).

Default behavior: the entity's stream URL is built from the cloud gateway
unless the user overrides it in the integration options:
  1. rtsp_url      - full URL override (e.g. go2rtc restream endpoint)
  2. rtsp_host     - LAN address (rtsp://<user>:<pass>@<host>:554/...)
  3. (default)     - cloud stream gateway stored at setup time

Dahua RTSP URI: rtsp://<user>:<pass>@<host>:<port>/cam/realmonitor?channel=N&subtype=0|1
"""

from __future__ import annotations

import json
import logging

import aiohttp

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_CHANNEL_JSON,
    CONF_DEVICE_ID,
    CONF_RTSP_HOST,
    CONF_RTSP_PASSWORD,
    CONF_RTSP_PORT,
    CONF_RTSP_SUBTYPE,
    CONF_RTSP_URL,
    CONF_RTSP_USERNAME,
    CONF_STREAM_ENTRY,
    DOMAIN,
)
from .streams import build_rtsp_url, is_lan_host, split_host_port

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up one camera entity per camera channel."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    device_id = entry.data[CONF_DEVICE_ID]

    channels = []
    try:
        channels = json.loads(entry.data.get(CONF_CHANNEL_JSON, "[]") or "[]")
    except (TypeError, json.JSONDecodeError):
        channels = []

    if not channels:
        channels = [{"channelId": "0", "channelName": "主摄像头", "functions": []}]
    elif all(not str(c.get("channelId", "")) for c in channels):
        channels = [{"channelId": "0", "channelName": "主摄像头", "functions": []}]

    entities = [
        LeChangeCameraEntity(coordinator, device_id, str(ch.get("channelId", "0")))
        for ch in channels
    ]
    async_add_entities(entities, True)


class LeChangeCameraEntity(CoordinatorEntity, Camera):
    """Camera entity for one doorbell channel of the lock."""

    _attr_has_entity_name = True
    _attr_translation_key = "camera"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, coordinator, device_id: str, channel_id: str) -> None:
        super().__init__(coordinator)
        Camera.__init__(self)
        self._device_id = device_id
        self._channel_id = channel_id
        self._attr_unique_id = f"{device_id}_camera_{channel_id}"
        self._attr_device_info = {"identifiers": {(DOMAIN, device_id)}}
        self._attr_translation_placeholders = {"channel_id": channel_id}

    # ---- helpers ----------------------------------------------------------
    @property
    def _options(self) -> dict:
        return self.coordinator.entry.options or {}

    @property
    def cloud_stream_entry(self) -> str:
        """Cloud stream gateway captured at setup (host[:port])."""
        return str(self.coordinator.entry.data.get(CONF_STREAM_ENTRY) or "").strip()

    def _host_and_port(self) -> tuple[str, int]:
        """Effective (host, port): options rtsp_host > cloud gateway default."""
        opts = self._options
        host = str(opts.get(CONF_RTSP_HOST) or "").strip()
        port = int(opts.get(CONF_RTSP_PORT, 554))
        if host:
            host, port = split_host_port(host, port)
            return host, port
        if self.cloud_stream_entry:
            # 云端流媒体网关默认走 TLS/443
            return split_host_port(self.cloud_stream_entry, 443)
        return "", port

    @property
    def is_streaming(self) -> bool:
        return bool(self.stream_source)

    @property
    def stream_source(self) -> str | None:
        """Return the RTSP/restream source URL for this channel."""
        opts = self._options
        url = str(opts.get(CONF_RTSP_URL) or "").strip()
        if url:
            return url.replace("{channel}", str(self._channel_id))
        host, port = self._host_and_port()
        if not host:
            return None
        user = str(opts.get(CONF_RTSP_USERNAME, "admin")).strip()
        pw = str(opts.get(CONF_RTSP_PASSWORD, "")).strip()
        subtype = int(opts.get(CONF_RTSP_SUBTYPE, 0))
        return build_rtsp_url(host, port, user, pw, self._channel_id, subtype)

    @property
    def extra_state_attributes(self) -> dict:
        attrs = {
            "channel_id": self._channel_id,
            "cloud_stream_entry": self.cloud_stream_entry,
        }
        data = self.coordinator.data or {}
        ch_names = data.get("channel_names") or {}
        if self._channel_id in ch_names:
            attrs["channel_name"] = ch_names[self._channel_id]
        channels = data.get("channels") or []
        for ch in channels:
            if str(ch.get("channelId")) == str(self._channel_id):
                attrs["functions"] = ch.get("functions") or []
                attrs["status"] = ch.get("status")
        wifi = data.get("wifi")
        if wifi:
            attrs["wifi_ssid"] = wifi.get("ssid")
            attrs["wifi_signal"] = wifi.get("intensity")
        return attrs

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Snapshot via the Dahua CGI on the LAN (cloud gateway has no CGI).

        Best effort: only when the effective host is a LAN IPv4 address.
        """
        host, _ = self._host_and_port()
        if not host or not is_lan_host(host):
            return None
        opts = self._options
        user = str(opts.get(CONF_RTSP_USERNAME, "admin")).strip()
        pw = str(opts.get(CONF_RTSP_PASSWORD, "")).strip()
        channel = int(self._channel_id) + 1
        url = f"http://{host}/cgi-bin/snapshot.cgi?channel={channel}&subtype=0"
        try:
            auth = aiohttp.BasicAuth(user, pw) if user else None
            resp = await self.coordinator.api._session.get(url, auth=auth, timeout=10)
            if resp.status == 200:
                return await resp.read()
            _LOGGER.debug("Snapshot HTTP %s from %s", resp.status, host)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Snapshot failed: %s", err)
        return None
