"""DataUpdateCoordinator for the LeChange (Imou) client-side cloud API."""

from __future__ import annotations

import json
import logging
import os
import random
import uuid
from datetime import timedelta
from typing import Any, Optional

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_API_HOST,
    CONF_INTERNAL_USERNAME,
    CONF_LAST_SNAPKEY_RESULT,
    CONF_PASSWORD,
    CONF_PRODUCT_ID,
    CONF_SESSION_ID,
    CONF_SNAPKEY_CONFIG,
    CONF_TOKEN,
    CONF_USERNAME,
    DEFAULT_SNAPKEY_CONFIG,
    DOMAIN,
    DEFAULT_SCAN_INTERVAL,
    EVENT_PREFIX,
    STATUS_ONLINE,
    STATUS_SLEEP,
    PROP_LOCK_NOTE_REPORT,
    PROP_CHANNEL_NAMES,
)
from .imou_client import ImouAPIError, ImouClient
from .media import MediaManager
from .mqtt import MqttManager
from .state_utils import (
    build_snapkey_periods,
    build_usage_period,
    derive_lock_state,
    extract_batteries,
    normalize_wifi,
    _int_or_none,
)

_LOGGER = logging.getLogger(__name__)


class LeChangeDataUpdateCoordinator(DataUpdateCoordinator):
    """Poll lock properties + device status from the Imou cloud."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self.hass = hass
        self.entry = entry

        self.device_id: str = entry.data[CONF_DEVICE_ID]
        self.product_id: str = entry.data.get(CONF_PRODUCT_ID, "")
        self.channel_id: str = "0"

        client_session = async_get_clientsession(hass)
        # 独立终端标识:安装时生成一次并持久化(不与手机 App 同终端 → 不互相顶号)
        # v1.4.1+:迁移为 App 同款标准 UUID(旧 lechange-hass-* 为 PC 时代格式)
        terminal_id = str((entry.options or {}).get("terminal_id", ""))
        if not terminal_id or terminal_id.startswith("lechange-hass-"):
            terminal_id = str(uuid.uuid4()).upper()
            hass.config_entries.async_update_entry(
                entry, options={**(entry.options or {}), "terminal_id": terminal_id}
            )
        self.api = ImouClient(
            client_session,
            username=entry.data.get(CONF_USERNAME, ""),
            password=entry.data.get(CONF_PASSWORD, ""),
            session_id=entry.data.get(CONF_SESSION_ID, ""),
            token=entry.data.get(CONF_TOKEN, ""),
            internal_username=entry.data.get(CONF_INTERNAL_USERNAME, ""),
            api_host=entry.data.get(CONF_API_HOST, ""),
            on_session_update=self._persist_session,
            terminal_id=terminal_id,
        )

        # 云端媒体管理(门外截图节流/缓存 + 告警图解码, 依赖安全码/设备密码)
        self.media = MediaManager(self)

        # MQTT 实时通道: 状态推送 + 控制优先 MQTT, 云 API 兜底
        self.mqtt = MqttManager(
            self.api,
            self.device_id,
            self.product_id,
            cloud_ctrl=self._mqtt_cloud_ctrl,
            on_event=self._mqtt_on_event,
            certs_dir=os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs"),
        )

        self._seen_lock_notes: set[str] = set()
        self._seen_alarm_ids: set[int] = set()
        self._alarm_seen_initialized = False
        self._device_info_update_unsub = None

        # 临时密码(实体化配置):从 entry.options 恢复
        self.snapkey_list: list[dict] = []
        saved_result = entry.options.get(CONF_LAST_SNAPKEY_RESULT)
        self.last_snapkey_result: Optional[dict] = (
            dict(saved_result) if isinstance(saved_result, dict) else None
        )

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{self.device_id}",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )

        # 每天刷新一次设备注册表型号/固件
        @callback
        def _schedule(now=None):
            if self._device_info_update_unsub:
                self._device_info_update_unsub()
            self._device_info_update_unsub = async_track_time_interval(
                hass, self._async_update_device_info, timedelta(days=1)
            )

        if hass.is_running:
            _schedule()
        else:
            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _schedule)

    @callback
    def _persist_session(self, session: dict) -> None:
        """Persist refreshed session data into the config entry."""
        data = dict(self.entry.data)
        data.update(session)
        self.hass.config_entries.async_update_entry(self.entry, data=data)
        _LOGGER.debug("Persisted new session")

    @property
    def device_name(self) -> str:
        return self.entry.data.get(CONF_DEVICE_NAME) or self.entry.title

    async def async_get_device_snapshot(self) -> Optional[dict]:
        """Fetch this device's full info (device.info.BasicInfoGet) with fallback."""
        try:
            return await self.api.async_get_device_info(self.device_id, self.product_id)
        except ImouAPIError as err:
            _LOGGER.debug("BasicInfoGet failed (%s), falling back to device list", err)
            devices = await self.api.async_get_devices()
            for dev in devices:
                if dev["deviceId"] == self.device_id:
                    return dev
            return None

    async def _async_update_data(self) -> dict:
        """Fetch device status + all IoT properties."""
        data: dict[str, Any] = {"last_error": None}
        props: dict[str, Any] = {}

        try:
            snapshot = await self.async_get_device_snapshot()
            if snapshot:
                data["status"] = snapshot.get("status", "")
                data["lock_state"] = snapshot.get("lockState", "")
                data["channels"] = snapshot.get("channels", [])
                data["online"] = snapshot.get("status") == STATUS_ONLINE
                data["sleeping"] = snapshot.get("status") == STATUS_SLEEP
                data["properties_map"] = snapshot.get("properties_map", "")
            else:
                data["status"] = None
                data["online"] = False
                data["sleeping"] = True
        except ImouAPIError as err:
            _LOGGER.warning("Device list failed: %s", err)
            data["last_error"] = str(err)
            data["online"] = False
            data["sleeping"] = True
            return data

        # 设备休眠时 IoT 调用会返回 10003,此时保留上次状态
        try:
            props = await self.api.async_get_properties(
                self.device_id, self.product_id, self.channel_id
            )
        except ImouAPIError as err:
            _LOGGER.warning("GetProperties failed: %s", err)
            data["last_error"] = str(err)
            data["props_ok"] = False
            # 列表接口自带的 propertiesMap 是设备最近上报快照(设备休眠也可用)
            props = await self._decode_properties_map(data.get("properties_map", ""))
        else:
            data["props_ok"] = True

        data["props"] = props

        # ---- derived fields ------------------------------------------------
        data["door_lock_status"] = _int_or_none(props.get("doorLockStatus"))
        data["battery_lock"], data["battery_camera"] = extract_batteries(props)
        data["wifi"] = normalize_wifi(props)

        notes = props.get(PROP_LOCK_NOTE_REPORT) or []
        if isinstance(notes, list):
            data["lock_notes"] = notes
            data["latest_open_door_record"] = (
                dict(notes[-1]) if notes and isinstance(notes[-1], dict) else {}
            )
            self._fire_new_lock_notes(notes)
        else:
            data["lock_notes"] = []
            data["latest_open_door_record"] = {}

        ch_names = props.get(PROP_CHANNEL_NAMES) or []
        if isinstance(ch_names, list):
            data["channel_names"] = {
                str(c.get("chn")): c.get("name", "") for c in ch_names if isinstance(c, dict)
            }
        else:
            data["channel_names"] = {}

        # ---- 云侧告警(设备休眠也可用) ------------------------------------
        try:
            alarm_data = await self.api.async_get_alarm_messages(
                self.device_id, self.product_id, self.channel_id
            )
            alarms = alarm_data.get("alarms") or []
            if isinstance(alarms, list):
                data["alarms"] = [a for a in alarms if isinstance(a, dict)][-20:]
                data["latest_alarm"] = data["alarms"][-1] if data["alarms"] else None
                self._fire_new_alarms(data["alarms"])
        except ImouAPIError as err:
            _LOGGER.debug("Alarm messages unavailable: %s", err)

        _LOGGER.debug("Coordinator data updated: %s", data)
        return data

    async def _decode_properties_map(self, raw: str) -> dict:
        """Decode the device-list propertiesMap (ref-keyed snapshot) via model."""
        if not raw:
            return {}
        try:
            pm = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(pm, dict):
                return {}
            model = await self.api.async_get_model(self.device_id, self.product_id)
            return model.decode_properties(pm)
        except (ImouAPIError, TypeError, ValueError, json.JSONDecodeError) as err:
            _LOGGER.debug("propertiesMap decode failed: %s", err)
            return {}

    def _fire_new_lock_notes(self, notes: list) -> None:
        """Fire an event for each newly seen door-open record."""
        for note in notes:
            if not isinstance(note, dict):
                continue
            key = json.dumps(note, ensure_ascii=False)
            if key in self._seen_lock_notes:
                continue
            self._seen_lock_notes.add(key)
            record = dict(note)
            record["device_id"] = self.device_id
            self.hass.bus.async_fire(
                EVENT_PREFIX, {"type": "open_record", "record": record}
            )

    def _fire_new_alarms(self, alarms: list) -> None:
        """Fire an event for each newly seen cloud alarm (backlog suppressed)."""
        new_ids = {a.get("alarmId") for a in alarms}
        new_ids.discard(None)
        if not self._alarm_seen_initialized:
            # 首次轮询只建立基线,不触发历史告警
            self._seen_alarm_ids = set(new_ids)
            self._alarm_seen_initialized = True
            return
        for alarm in alarms:
            alarm_id = alarm.get("alarmId")
            if alarm_id is None or alarm_id in self._seen_alarm_ids:
                continue
            self._seen_alarm_ids.add(alarm_id)
            payload = dict(alarm)
            payload["device_id"] = self.device_id
            self.hass.bus.async_fire(
                EVENT_PREFIX, {"type": "alarm", "alarm": payload}
            )

    # -------------------------------------------------------- 临时密码配置
    @property
    def snapkey_config(self) -> dict:
        """Current temporary-password config (defaults + persisted options)."""
        config = dict(DEFAULT_SNAPKEY_CONFIG)
        saved = self.entry.options.get(CONF_SNAPKEY_CONFIG)
        if isinstance(saved, dict):
            for key, value in saved.items():
                if key in config:
                    config[key] = value
        return config

    def update_snapkey_config(self, **updates) -> None:
        """Persist temporary-password settings into entry options."""
        config = self.snapkey_config
        config.update(updates)
        self.hass.config_entries.async_update_entry(
            self.entry,
            options={**self.entry.options, CONF_SNAPKEY_CONFIG: config},
        )

    def get_snapkey_periods(self, config: Optional[dict] = None) -> list[dict]:
        """Build CreateDeviceSnapkey effectPeriod from the persisted settings."""
        config = config or self.snapkey_config
        return build_snapkey_periods(
            str(config.get("begin_time", "00:00:00")),
            str(config.get("end_time", "23:59:59")),
            str(config.get("weekday_mode", "Every day")),
        )

    def set_snapkey_result(self, result: dict) -> None:
        """Remember the last generated password (minimal fields) in options."""
        if not isinstance(result, dict):
            self.last_snapkey_result = None
            return
        self.last_snapkey_result = {
            "name": result.get("name"),
            "key": result.get("key"),
        }
        self.hass.config_entries.async_update_entry(
            self.entry,
            options={
                **self.entry.options,
                CONF_LAST_SNAPKEY_RESULT: self.last_snapkey_result,
            },
        )

    def set_snapkey_list(self, keys: list) -> None:
        """Remember the last fetched temporary-password list."""
        self.snapkey_list = list(keys) if isinstance(keys, list) else []

    def update_media_options(self, **updates) -> None:
        """Persist cloud-media settings (通道/布局/OSD/节流) into entry options."""
        self.hass.config_entries.async_update_entry(
            self.entry,
            options={**self.entry.options, **updates},
        )

    async def async_create_snapkey_cloud(self, config: Optional[dict] = None) -> dict:
        """生成临时密码:客户端自产 keyId/tempKey,
        经消息域 `iot.message.SmartLockSecretAdd` 直接登记(不加 SetService,
        不触发身份验证码;设备休眠也可用,实测 10000)。

        成功后返回 {"name", "key": tempKey, ...} 供结果展示。
        """
        config = config or self.snapkey_config
        temp_key = f"{random.randint(0, 99999999):08d}"
        usage_period = build_usage_period(
            str(config.get("weekday_mode", "Every day")),
            int(config.get("effective_day", 1)),
        )
        result = await self.api.async_smart_lock_secret_add(
            self.device_id,
            self.product_id,
            temp_key,
            name=str(config.get("name", "Home Assistant")),
            number=int(config.get("effective_num", -1)),
            effect_days=int(config.get("effective_day", 1)),
            usage_period=usage_period,
        )
        return {
            "name": config.get("name", ""),
            "key": temp_key,
            "usage_period": usage_period,
            "raw": result,
        }

    @property
    def is_locked(self) -> Optional[bool]:
        """Derive HA lock state from doorLockStatus/doorLockState/lockState."""
        data = self.data or {}
        props = data.get("props", {})
        return derive_lock_state(
            data.get("door_lock_status"),
            props.get("doorLockState"),
            data.get("lock_state", ""),
        )

    async def _async_update_device_info(self, now=None) -> None:
        """Update the device registry entry (model / firmware)."""
        try:
            snapshot = await self.async_get_device_snapshot()
        except ImouAPIError as err:
            _LOGGER.debug("Device info refresh failed: %s", err)
            return
        if not snapshot:
            return

        registry = dr.async_get(self.hass)
        device_entry = registry.async_get_device(identifiers={(DOMAIN, self.device_id)})
        if not device_entry:
            return

        update: dict = {}
        if snapshot.get("model"):
            update["model"] = snapshot["model"]
        if snapshot.get("version"):
            update["sw_version"] = snapshot["version"]
        if snapshot.get("name"):
            update["name"] = snapshot["name"]
        if update:
            registry.async_update_device(device_entry.id, **update)

    async def async_update_device_info(self) -> None:
        """Public: refresh model/firmware in device registry."""
        await self._async_update_device_info()

    # ------------------------------------------------------------------ MQTT
    async def async_iot_control(self, api: str, payload: dict) -> dict:
        """IoT 控制统一入口: MQTT 优先 → 云 API 兜底.

        payload 需已 ref 编码(调用方用 model 编码); 返回 dict, 带 "via" 标记。
        MQTT 未连接/超时 → 云 API(imou_client.async_post); 业务错误直接抛异常。
        """
        try:
            return await self.mqtt.async_request(api, payload)
        except ImouAPIError:
            raise  # MQTT 业务错误(设备离线等): 不兜底, 交由调用方处理
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("MQTT control failed (%s); falling back to cloud", err)
            data = await self.api.async_post(api, payload)
            if isinstance(data, dict):
                data = dict(data)
                data["via"] = "cloud"
            return data

    async def _mqtt_cloud_ctrl(self, api: str, params: dict) -> dict:
        """MQTT 控制失败时的云 API 兜底(SetService/SetProperties 统一入口)."""
        return await self.api.async_post(api, params)

    async def _mqtt_on_event(self, data: dict) -> None:
        """MQTT 推送 → 解码属性 → 实时更新 data/实体(完整数据仍靠轮询兜底)."""
        topic = data.get("topic") or ""
        msg = data.get("msg") or {}
        if topic == "iot_response":
            inner = (msg.get("params") or {}).get("data") or {}
            props = inner.get("properties") or {}
            if props:
                try:
                    model = await self.api.async_get_model(
                        self.device_id, self.product_id
                    )
                    decoded = model.decode_properties(props) if isinstance(props, dict) else {}
                    # 合并实时属性到 data(实体 is_on 直接读取)
                    new_data = dict(self.data or {})
                    new_data["_mqtt_props"] = decoded
                    new_data.setdefault("props", {}).update(decoded)
                    new_data["mqtt_online"] = True
                    self.async_set_updated_data(new_data)
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("MQTT props decode/update failed", exc_info=True)
            elif inner.get("code") == 10000:
                _LOGGER.debug("MQTT response: %s", str(inner)[:200])
        elif topic == "android_iot_property":
            # 属性变更推送(如 device online/offline)
            props = msg.get("data") or msg.get("params") or {}
            if props:
                try:
                    model = await self.api.async_get_model(
                        self.device_id, self.product_id
                    )
                    decoded = model.decode_properties(props) if isinstance(props, dict) else {}
                    if decoded:
                        new_data = dict(self.data or {})
                        new_data.setdefault("props", {}).update(decoded)
                        self.async_set_updated_data(new_data)
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("MQTT property push decode failed", exc_info=True)
        _LOGGER.debug("MQTT event: topic=%s keys=%s", topic, list(msg.keys())[:5])

    async def async_start_mqtt(self) -> None:
        """启动 MQTT 实时通道(首次轮询后调用; 幂等)."""
        await self.mqtt.async_start()

    async def async_shutdown(self) -> None:
        """Cleanup."""
        if self._device_info_update_unsub:
            self._device_info_update_unsub()
            self._device_info_update_unsub = None
        await self.mqtt.async_stop()
