"""DataUpdateCoordinator for the LeChange (Imou) client-side cloud API."""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
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
    build_usage_period,
    derive_lock_state,
    extract_batteries,
    extract_latest_open_record,
    inherit_previous,
    normalize_wifi,
    sort_records_by_time,
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
        # ★ 必须与 config_flow 授权链所用 terminal_id 完全一致 — 服务端按
        #   UA terminalId 记忆授权清单;不一致则重登时 12112 → MQTT 12001 循环。
        # ★ 任何非空 tid 都直接使用(包括旧版 lechange-hass- 前缀) ——
        #   那就是历史已授权终端;重新生成 = 12112 → 强制 reauth →
        #   用户终端管理列表 +1(升级即换终端的 BUG, 已修)。
        #   空 tid 的兜底生成在 async_setup_entry(异步)里走安装级存储链。
        terminal_id = str((entry.data or {}).get("terminal_id")
                          or (entry.options or {}).get("terminal_id") or "")
        if terminal_id and terminal_id.startswith("lechange-hass-"):
            _LOGGER.warning(
                "Using legacy terminal_id %s… (already authorized); "
                "do NOT regenerate", terminal_id[:8],
            )
        if not terminal_id:
            # 仅当条目完全没有 tid 时兜底(正常路径由 async_setup_entry
            # 的安装级存储解析提前写入)
            terminal_id = str(uuid.uuid4()).upper()
            hass.config_entries.async_update_entry(
                entry, options={**(entry.options or {}), "terminal_id": terminal_id}
            )
        # ★ 账号级共享运行时: 同账号同终端的条目共用一条云会话与一条 MQTT
        #   连接(对齐真机 App 形态)。各自登录会互踢单活跃 token(10001)，
        #   各自 MQTT 建连会因 clientId(terminal 派生)相同互相接管踢线 ——
        #   这两类都是多设备场景的"数据干扰"根源。共享后推送按 deviceId
        #   归属分发(AccountMqttHub), 设备层数据仍然严格隔离。
        #   terminal_id 不同的 legacy 条目回退旧行为(独立会话/连接)。
        from .account_runtime import resolve_account_runtime

        username = str(entry.data.get(CONF_USERNAME, "") or "")
        self._account_runtime, _shared = resolve_account_runtime(
            hass, username, terminal_id
        )
        if _shared:
            self.api = self._account_runtime.ensure_client(
                client_session, entry, terminal_id
            )
            # 回调接账号级扇出(共享 client 只有一组回调槽位, 不能让后
            # setup 的条目覆盖前条目的): 会话持久化 → 全条目扇出;
            # 12112 终端拦截 → 广播给所有活动条目(各自 reauth 引导)。
            self._account_runtime.bind_client_callbacks(self.api)
            self._account_runtime.add_block_listener(self._on_login_blocked)
            self._account_runtime.mark_entry_active(entry.entry_id)
        else:
            self.api = ImouClient(
                client_session,
                username=entry.data.get(CONF_USERNAME, ""),
                password=entry.data.get(CONF_PASSWORD, ""),
                session_id=entry.data.get(CONF_SESSION_ID, ""),
                token=entry.data.get(CONF_TOKEN, ""),
                internal_username=entry.data.get(CONF_INTERNAL_USERNAME, ""),
                api_host=entry.data.get(CONF_API_HOST, ""),
                on_session_update=self._persist_session,
                on_login_blocked=self._on_login_blocked,
                terminal_id=terminal_id,
            )

        # 云端媒体管理(门外截图节流/缓存 + 告警图解码, 依赖安全码/设备密码)
        self.media = MediaManager(self)

        # MQTT 实时通道: 状态推送 + 控制优先 MQTT, 云 API 兜底
        # 共享模式 → 条目级 facade(背后是账号级 hub, 按 deviceId 分发);
        # 隔离模式(legacy 混合终端) → 独立 MqttManager(原行为)。
        if _shared:
            self.mqtt = self._account_runtime.facade(self.device_id)
            self.mqtt.bind(self._mqtt_on_event)
        else:
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

    def _on_login_blocked(self, code: int) -> None:
        """登录被终端管理拦截(12112/12001) → 一次性引导重新认证(reauth)."""
        if getattr(self, "_reauth_started", False):
            return
        self._reauth_started = True
        _LOGGER.warning(
            "Terminal authorization required (code %s) — starting reauth flow "
            "for entry %s", code, self.entry.entry_id,
        )
        try:
            self.entry.async_start_reauth(self.hass)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Starting reauth failed")

    @property
    def device_name(self) -> str:
        return self.entry.data.get(CONF_DEVICE_NAME) or self.entry.title

    async def ensure_awake(self, max_wait: float = 15.0) -> bool:
        """确保设备在线(休眠则经取流请求唤醒, 该请求自带唤醒语义).

        用于属性写入前(SetProperties 是设备面命令, 设备休眠时必然
        19999/device error -1)。返回 True=设备已在线(或本就在线)。
        """
        try:
            snapshot = await self.async_get_device_snapshot()
            status = (snapshot or {}).get("status")
            if status == STATUS_ONLINE:
                return True
            if status and status != STATUS_SLEEP:
                # 未知状态: 也尝试继续(不阻塞)
                return True
            # 唤醒: 取流 URL 请求自带唤醒语义(~5s 上线), 不真正取流
            try:
                await self.api.async_get_transfer_stream_url(
                    self.device_id, self.product_id, "0", "1"
                )
            except Exception as err:  # noqa: BLE001 — 唤醒尽力而为
                _LOGGER.debug("Wake request failed (continuing): %s", err)
            # 轮询等设备上线
            import asyncio as _aio

            waited = 0.0
            while waited < max_wait:
                await _aio.sleep(2.0)
                waited += 2.0
                try:
                    snapshot = await self.async_get_device_snapshot()
                    if (snapshot or {}).get("status") == STATUS_ONLINE:
                        _LOGGER.info("Device %s awake after %.0fs", self.device_id, waited)
                        return True
                except Exception:  # noqa: BLE001
                    pass
            _LOGGER.warning(
                "Device %s still sleeping after %.0fs wake wait",
                self.device_id, max_wait,
            )
            return False
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("ensure_awake failed: %s", err)
            return False

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
        prev = self.data if isinstance(self.data, dict) else None
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
            # 休眠/网络抖动: 继承上次展示数据(实体不清空), 仅错误状态更新
            return inherit_previous(prev, data)

        # ★ 主路径: 带显式属性列表的 GetProperties(App 抓包同款,
        #   timeout=15s + qos=1)。裸调不带列表时服务端立即空回(休眠
        #   设备 10003/空 properties) — 这是"电量有(列表快照缓存)而
        #   童锁/WiFi/门状态全缺"的根因。带列表后服务端会等待设备
        #   应答, 在线/唤醒窗口内返回实时值。
        try:
            props = await self.api.async_get_properties(
                self.device_id, self.product_id, self.channel_id
            )
        except ImouAPIError as err:
            if err.code == 10003:
                _LOGGER.debug("GetProperties 10003 (device sleeping/asleep) — using snapshot")
            else:
                _LOGGER.warning("GetProperties failed: %s", err)
                data["last_error"] = str(err)
            data["props_ok"] = False
            # 降级: V2 快照 propertiesMap(设备最近上报, 休眠也可用)
            props = await self._decode_properties_map(data.get("properties_map", ""))
        else:
            data["props_ok"] = True

        # 降级链(核心键仍缺, 设备睡死/超时时, 60s 节流):
        # ① iot.manager.GetDeviceDetailInfo 云端属性缓存(App 设备页同款,
        #   抓包 20260905 实证: 76 个 ref, 休眠可读, 含 wifiDoorLock/
        #   doorLockState/devicePowerLock/通道名/sdl_inOpenDoorModel(童锁)
        #   — 注意 child_lock/doorLockStatus 为纯实时属性, 云端无缓存)
        core_keys = ("child_lock", "devicePowerLock", "wifiDoorLock", "doorLockStatus")
        missing = [
            k for k in core_keys
            if k not in props and not (k == "child_lock" and "sdl_inOpenDoorModel" in props)
        ]
        if missing:
            now = time.monotonic()
            # 首次尝试必须执行: 新分配的 CI runner / 刚启动的容器
            # monotonic() 可能 < 60s, 0.0 初值不能当"很久以前"
            if (
                not hasattr(self, "_last_fill_attempt")
                or now - self._last_fill_attempt >= 60.0
            ):
                self._last_fill_attempt = now
                filled: list[str] = []
                # ① GetDeviceDetailInfo 云端属性缓存(休眠可读)
                try:
                    detail = await self.api.async_get_device_detail_info(
                        self.device_id, self.product_id
                    )
                    detail_props = detail.get("properties") or {}
                    if isinstance(detail_props, dict) and detail_props:
                        decoded_detail = await self._decode_properties_map(
                            detail_props
                        )
                        for k, v in decoded_detail.items():
                            props.setdefault(k, v)
                        filled = [k for k in missing if k in props]
                        data["properties_map"] = json.dumps(
                            detail_props, ensure_ascii=False
                        )
                except Exception as err2:  # noqa: BLE001 — 兜底绝不失败收场
                    _LOGGER.debug("GetDeviceDetailInfo fallback failed: %s", err2)
                # ② ③ propertiesMap 快照(仍缺则再补)
                still = [k for k in missing if k not in props]
                if still:
                    snapshot_map = ""
                    try:
                        dev_info = await self.api.async_get_device_info(
                            self.device_id, self.product_id
                        )
                        snapshot_map = dev_info.get("properties_map") or ""
                    except Exception as err3:  # noqa: BLE001
                        _LOGGER.debug("BasicInfoGetV2 fallback failed: %s", err3)
                    if not snapshot_map:
                        try:
                            for dev in await self.api.async_get_devices():
                                if (dev.get("deviceId") == self.device_id
                                        and dev.get("properties_map")):
                                    snapshot_map = dev["properties_map"]
                                    break
                        except Exception as err4:  # noqa: BLE001
                            _LOGGER.debug("Device list fallback failed: %s", err4)
                    if snapshot_map:
                        decoded = await self._decode_properties_map(snapshot_map)
                        for k, v in decoded.items():
                            props.setdefault(k, v)
                        data["properties_map"] = snapshot_map
                        filled = [k for k in missing if k in props]
                _LOGGER.debug(
                    "Core-props fill from cache/snapshot: %s; still missing: %s",
                    filled or "none",
                    [k for k in missing if k not in props] or "none",
                )

        # 本次拿到属性才覆盖;空 props(休眠且快照缺)时保留上次值,
        # 避免 MQTT 刚推的实时状态/电量/记录被轮询清零。
        # ★ 合并而非整体替换: 某次轮询缺 sdl_inOpenDoorModel 等键(休眠/
        #   设备应答快)时覆盖会丢真值 → 状态闪跳(开关/传感器短暂变 None)。
        if props:
            current = data.get("props") or {}
            merged = dict(current)
            merged.update(props)
            data["props"] = merged

        # ---- derived fields(仅本次有对应原始字段时覆盖) --------------------
        if "doorLockStatus" in props:
            data["door_lock_status"] = _int_or_none(props.get("doorLockStatus"))
        if "devicePowerLock" in props:
            data["battery_lock"], data["battery_camera"] = extract_batteries(props)
        if "wifiDoorLock" in props:
            data["wifi"] = normalize_wifi(props)

        notes = props.get(PROP_LOCK_NOTE_REPORT) or []
        if isinstance(notes, list) and notes:
            data["lock_notes"] = notes
            data["latest_open_door_record"] = (
                dict(notes[-1]) if isinstance(notes[-1], dict) else {}
            )
            self._fire_new_lock_notes(notes)
        elif "lockNoteReport" in props:
            data["lock_notes"] = []
            data["latest_open_door_record"] = {}

        ch_names = props.get(PROP_CHANNEL_NAMES) or []
        if isinstance(ch_names, list) and ch_names:
            data["channel_names"] = {
                str(c.get("chn")): c.get("name", "") for c in ch_names if isinstance(c, dict)
            }
        elif PROP_CHANNEL_NAMES in props:
            data["channel_names"] = {}

        # ---- 云侧告警(设备休眠也可用) ------------------------------------
        try:
            alarm_data = await self.api.async_get_alarm_messages(
                self.device_id, self.product_id, self.channel_id
            )
            alarms = alarm_data.get("alarms") or []
            if isinstance(alarms, list):
                # ★ 按时间升序稳定排序: 云 API 返回顺序未文档化, 盲取末条
                # 当最新会拿到旧记录(显示时间不对的根因); 缺时间的沉底。
                valid = sort_records_by_time(
                    [a for a in alarms if isinstance(a, dict)]
                )
                data["alarms"] = valid[-20:]
                data["latest_alarm"] = valid[-1] if valid else None
                self._fire_new_alarms(data["alarms"])
        except ImouAPIError as err:
            _LOGGER.debug("Alarm messages unavailable: %s", err)

        # ---- 临时密码列表(云侧, 设备休眠也可用; 每轮询自动刷新) ------------
        try:
            secret = await self.api.async_smart_lock_secret_list(
                self.device_id, self.product_id, types=3
            )
            # secrets[] 才是临时密码(secretGroups[] 是 80 个固定分组槽位)
            if isinstance(secret, dict):
                self.set_snapkey_list(secret.get("secrets") or [])
        except ImouAPIError as err:
            _LOGGER.debug("Secret list unavailable: %s", err)

        # ---- 开门记录双通道合并(属性 vs 云侧告警, 休眠期告警兜底) ----------
        # 传整批告警: 内部过滤开门类 + 按时间取最新(不依赖 API 顺序,
        # 移动侦测/门铃等非开门告警不会混入"最近开门")
        merged = extract_latest_open_record(
            data.get("latest_open_door_record"), data.get("alarms")
        )
        if merged:
            data["latest_open_door_record"] = merged

        # 休眠韧性收尾: 本次未产出的展示字段继承上次值(绝不清零)
        return inherit_previous(prev, data)

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
            # ★ sort_keys: 同一记录两次上报字段顺序不同会生成不同键 → 重复事件
            key = json.dumps(note, ensure_ascii=False, sort_keys=True)
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

    # -------------------------------------------------------- 属性文本语言
    @property
    def language(self) -> str:
        """属性展示文本语言(HA 实例语言).

        HA 前端不翻译属性内容 —— 词映射由集成按 hass.config.language
        产出(i18n.py 目录); 实时读取, 用户改语言无需重载集成。
        """
        try:
            return str(getattr(self.hass.config, "language", "") or "")
        except Exception:  # noqa: BLE001 — 测试桩/异常环境兜底
            return ""

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
        """生成临时密码(App 同链路两步式优先).

        ① SetService CreateDeviceSnapkey —— **服务端签发** key/keyID
           (bundle 逆向证实 App 即此链路;设备侧流水因此知晓密钥,
           state 0→1 可确认, App 端增删语义一致);
        ② 消息域 SmartLockSecretAdd 固化登记(服务端 keyId/tempKey)。

        设备离线/模型无此服务(10003 等)→ 回落客户端自产
        keyId/tempKey(记录 state 恒 0, App 端不可删, 到期自动清理;
        返回值 mode 字段区分两种出身)。

        成功后返回 {"name", "key": tempKey, "mode": server|client, ...}。
        """
        config = config or self.snapkey_config
        name = str(config.get("name", "Home Assistant"))
        number = int(config.get("effective_num", -1))
        effect_days = int(config.get("effective_day", 1))

        # ★ 生成前先唤醒设备(取流 URL 自带唤醒语义, 与 SetProperties 不同):
        #   CreateDeviceSnapkey 是设备面命令 —— 设备在线才成功(step① → 服务端
        #   签发 key/keyID → step② Add state=1, App 可删/认可); 设备休眠时 step①
        #   → 10003 → 回落 state=0 草稿(App 删除返回 10000 但列表不移除)。
        #   ensure_awake 尽力而为(唤醒失败不阻断, 回落仍可登记)。
        if not await self.ensure_awake(max_wait=10.0):
            _LOGGER.info(
                "Device %s still sleeping; snapkey will be state=0 draft "
                "(effective once device online; App-delete deferred)",
                self.device_id,
            )

        # ★ 两步式优先(App 同款, 逆向 main.jsbundle addTempKeyToSass @11739016
        #   铁证: setService(CreateDeviceSnapkey).then(keyAdd)): ①SetService
        #   CreateDeviceSnapkey(服务端签发 key/keyID, **设备知晓 → 服务端认可**)
        #   → ②SmartLockSecretAdd 登记(带完整 usagePeriod)。
        #   ★ "入口=认可": 一步式(客户端自产 keyId)设备数据库不知晓 → state=0
        #   未认可, 设备同步时被服务端清理; 两步式(服务端签发)设备知晓 →
        #   state=1 认可, 不被清理(用户实测 HA→App 生成序列证实)。
        try:
            created = await self.api.async_smart_lock_create_snapkey(
                self.device_id,
                self.product_id,
                name=name,
                number=number,
                effect_days=effect_days,
            )
            usage_period = build_usage_period(effect_days)
            return {
                "name": config.get("name", ""),
                "key": created["temp_key"],
                "key_id": created["key_id"],
                "mode": "server",
                "usage_period": usage_period,
                "raw": created["raw"],
            }
        except ImouAPIError as err:
            _LOGGER.info(
                "CreateDeviceSnapkey unavailable (%s); falling back to "
                "client-generated keyId (state=0 draft; device online → state=1)",
                err,
            )

        # ② 回落: 客户端自产(8 位内 keyId, App 模型范围) + 消息域登记
        # ★ 密码类值必须用 secrets(加密安全 RNG), random(MT) 可预测
        key_id = secrets.randbelow(2000000) + 67000000  # App 段 [67000000, 68999999]
        temp_key = f"{secrets.randbelow(100000000):08d}"
        mode = "client"

        # 生效星期系统自动分配(每天, 掩码 127); 生效窗口由有效天数决定
        usage_period = build_usage_period(effect_days)
        result = await self.api.async_smart_lock_secret_add(
            self.device_id,
            self.product_id,
            temp_key,
            name=name,
            number=number,
            effect_days=effect_days,
            usage_period=usage_period,
            key_id=key_id,
        )
        return {
            "name": config.get("name", ""),
            "key": temp_key,
            "key_id": key_id,
            "mode": mode,
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
        # HA 2026.7+: async_get_device 弃用(标识符跨 entry 不再唯一),
        # 新 API 为 async_get_device_by_identifier(identifier, config_entry_id);
        # 旧版本回退 async_get_device(identifiers=...)
        getter = getattr(registry, "async_get_device_by_identifier", None)
        if getter is not None:
            device_entry = getter(
                (DOMAIN, self.device_id), self.entry.entry_id
            )
        else:
            device_entry = registry.async_get_device(
                identifiers={(DOMAIN, self.device_id)}
            )
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
        """IoT 控制/设置统一入口 —— **一律走云 API(HTTP)**.

        ★ MQTT 写通道已废弃(真机 2026-09 实证): App 业务 21 抓包全走
          HTTP, MQTT 零使用; MQTT 连接需私有 CA 且 CONNACK 前被服务端
          零字节断开(实验性质通道, 凭证正确仍被拒)。设置/控制/生成
          类操作统一走 imou_client.async_post(HTTP 云 API)。
        返回 dict, 带 "via": "cloud" 标记。
        """
        data = await self.api.async_post(api, payload)
        if isinstance(data, dict):
            data = dict(data)
            data["via"] = "cloud"
        return data

    async def _mqtt_cloud_ctrl(self, api: str, params: dict) -> dict:
        """MQTT 控制失败时的云 API 兜底(SetService/SetProperties 统一入口)."""
        return await self.api.async_post(api, params)

    async def _mqtt_on_event(self, data: dict) -> None:
        """MQTT 推送 → 解码属性 → 实时更新 data/实体(完整数据仍靠轮询兜底).

        ★ 数据隔离防线(与 mqtt.AccountMqttHub 的 deviceId 分发互补):
          推送先验归属 —— 只接受本条目 deviceId 的消息; 即使经 legacy
          独立连接或未来某层漏过他设备推送, 也绝不合并进本条目数据。
        """
        topic = data.get("topic") or ""
        msg = data.get("msg") or {}
        # 归属校验: iot_response 是自己请求的响应(已按 seq 消费, 正常不会
        # 到这里); android_iot_property 等推送必须携带本条目 deviceId。
        from .mqtt import extract_device_id

        pushed_device_id = extract_device_id(msg)
        if topic != "iot_response" and pushed_device_id and pushed_device_id != self.device_id:
            _LOGGER.debug(
                "MQTT push for %s… ignored (entry device %s…)",
                pushed_device_id[:6], self.device_id[:6],
            )
            return
        if topic == "iot_response":
            inner = (msg.get("params") or {}).get("data") or {}
            props = inner.get("properties") or {}
            if props:
                try:
                    model = await self.api.async_get_model(
                        self.device_id, self.product_id
                    )
                    decoded = model.decode_properties(props) if isinstance(props, dict) else {}
                    # 合并实时属性到 data(实体 is_on 直接读取)。
                    # 非变更合并: 传入的 new_data 直接写回时 triggers 轮询监
                    # 听器(CONST 警告刷屏); 仅在确有新值时提交。
                    current = dict(self.data or {})
                    merged_props = dict(current.get("props") or {})
                    merged_props.update(decoded)
                    changed = merged_props != current.get("props")
                    if changed:
                        new_data = dict(current)
                        new_data["_mqtt_props"] = decoded
                        new_data["props"] = merged_props
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
                        current = dict(self.data or {})
                        merged_props = dict(current.get("props") or {})
                        merged_props.update(decoded)
                        if merged_props != current.get("props"):
                            new_data = dict(current)
                            new_data["props"] = merged_props
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
        # 释放账号运行时的条目占位与回调注册; 账号下已无活动条目 →
        # 清理运行时, 共享会话凭据不残留内存(下次 setup 从持久化 entry 重建)
        try:
            from .account_runtime import drop_account_runtime_if_idle

            self._account_runtime.remove_block_listener(self._on_login_blocked)
            self._account_runtime.unmark_entry_active(self.entry.entry_id)
            drop_account_runtime_if_idle(self.hass, self.api.username)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Account runtime release failed", exc_info=True)
