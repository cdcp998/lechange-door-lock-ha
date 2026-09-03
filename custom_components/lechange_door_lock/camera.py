"""Camera platform (视频): 门外截图 + 云端预览。

链路优先级:
  1. rtsp_url / rtsp_host(局域网 Dahua CGI) — 用户显式覆盖时走传统路径
  2. 云端 RTSV1 门外截图(WI-007 协议, media.py 节流): 电池设备默认路径,
     取流请求自带唤醒 → 强制节流(默认 60s, options 可调), 区间内返回缓存帧

云端链路无法输出 RTSP URL(HA stream 组件需要), 故仅在配置了
rtsp 覆盖时声明 CameraEntityFeature.STREAM; 纯云端时前端按快照轮询。
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
    _attr_supported_features = CameraEntityFeature(0)

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
        """RTSP/restream URL(仅显式覆盖时; 纯云端走快照轮询)。"""
        opts = self._options
        url = str(opts.get(CONF_RTSP_URL) or "").strip()
        if url:
            return url.replace("{channel}", str(self._channel_id))
        host = str(opts.get(CONF_RTSP_HOST) or "").strip()
        if not host:
            return None  # 云端 RTSV1 无法映射为 HA stream 源
        port = int(opts.get(CONF_RTSP_PORT, 554))
        host, port = split_host_port(host, port)
        user = str(opts.get(CONF_RTSP_USERNAME, "admin")).strip()
        pw = str(opts.get(CONF_RTSP_PASSWORD, "")).strip()
        subtype = int(opts.get(CONF_RTSP_SUBTYPE, 0))
        return build_rtsp_url(host, port, user, pw, self._channel_id, subtype)

    @property
    def supported_features(self) -> CameraEntityFeature:
        """配置了 RTSP/中转源才支持 STREAM(供 HA stream 组件接管)。"""
        if self.stream_source:
            return CameraEntityFeature.STREAM
        return CameraEntityFeature(0)

    @property
    def extra_state_attributes(self) -> dict:
        attrs = {
            "channel_id": self._channel_id,
            "cloud_stream_entry": self.cloud_stream_entry,
            "cloud_snapshot": self.coordinator.media.security_code != "",
            "snapshot_min_interval": self.coordinator.media.snapshot_min_interval,
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
        """门外截图: 局域网 CGI(如配置) → 云端 RTSV1 抽帧(节流)。"""
        lan = await self.capture_image_via_options(self.coordinator, self._channel_id)
        if lan:
            return lan
        # 云端链路(电池设备默认): 内部节流, 区间内返回缓存帧
        return await self.coordinator.media.async_cloud_snapshot()

    @staticmethod
    async def capture_image_via_options(coordinator, channel_id: str) -> bytes | None:
        """按集成选项(rtsp_host/凭据)抓拍门外画面;返回图片字节或 None."""
        opts = coordinator.entry.options or {}
        host = str(opts.get(CONF_RTSP_HOST) or "").strip()
        if not host or not is_lan_host(host):
            return None
        user = str(opts.get(CONF_RTSP_USERNAME, "admin")).strip()
        pw = str(opts.get(CONF_RTSP_PASSWORD, "")).strip()
        channel = int(channel_id) + 1
        url = f"http://{host}/cgi-bin/snapshot.cgi?channel={channel}&subtype=0"
        try:
            auth = aiohttp.BasicAuth(user, pw) if user else None
            resp = await coordinator.api._session.get(url, auth=auth, timeout=10)
            if resp.status == 200:
                return await resp.read()
            _LOGGER.debug("Snapshot HTTP %s from %s", resp.status, host)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Snapshot failed: %s", err)
        return None
