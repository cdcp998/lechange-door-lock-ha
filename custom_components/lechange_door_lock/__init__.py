"""LeChange (Imou) door lock integration.

Uses the client-side cloud API of the official mobile clients
instead of the slow-to-update Open Platform API.
"""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    CONF_FIRMWARE_VERSION,
    CONF_MODEL_NAME,
    CONF_USERNAME,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import LeChangeDataUpdateCoordinator
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the integration (called by HA with configuration.yaml)."""
    # 幂等注册 GT4 滑块视图(纯 config-flow 场景由 config_flow 流程入口保证注册)
    from .gt4_helper import ensure_gt4_views_registered

    ensure_gt4_views_registered(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a LeChange door lock from a config entry."""
    _LOGGER.debug("Starting async_setup_entry for device %s", entry.data[CONF_DEVICE_ID])

    # ★ 账号运行时注册表挂载点(同账号条目共享会话/MQTT 连接, 见
    #   account_runtime)。setdefault 幂等: 纯 config-flow 场景首条目
    #   setup 前 hass.data[DOMAIN] 可能尚不存在(async_setup 不保证先跑)。
    hass.data.setdefault(DOMAIN, {})

    # ★ 终端指纹兜底: 条目缺 tid(极老版本安装)时先走安装级存储解析 ——
    #   命中已授信指纹则原位写回 entry(避免 coordinator 兜底随机生成 →
    #   12112 → reauth → 终端管理 +1)
    if not (entry.data or {}).get("terminal_id") and not (entry.options or {}).get("terminal_id"):
        try:
            from .terminal_store import get_or_create_terminal_id

            username = str(entry.data.get(CONF_USERNAME, "") or "")
            if username:
                tid = await get_or_create_terminal_id(hass, username)
                if tid:
                    hass.config_entries.async_update_entry(
                        entry, options={**(entry.options or {}), "terminal_id": tid}
                    )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Terminal id fallback resolution failed", exc_info=True)

    coordinator = LeChangeDataUpdateCoordinator(hass, entry)
    # 先存入 hass.data: 首刷失败/中断时 async_unload_entry 也能找到并清理
    hass.data[DOMAIN][entry.entry_id] = coordinator

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await coordinator.async_shutdown()
        hass.data[DOMAIN].pop(entry.entry_id, None)
        raise
    _LOGGER.debug("Coordinator first refresh completed")

    # 注册设备
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.data[CONF_DEVICE_ID])},
        name=entry.data.get(CONF_DEVICE_NAME) or entry.title,
        manufacturer="LeChange",
        model=entry.data.get(CONF_MODEL_NAME) or "SmartLock",
        sw_version=entry.data.get(CONF_FIRMWARE_VERSION),
        serial_number=entry.data[CONF_DEVICE_ID],
    )

    # 立即拉取一次型号/固件信息
    await coordinator.async_update_device_info()

    # 终端指纹迁移: entry 内当前(已授信)指纹落盘到安装级存储 —
    # 老版本安装的指纹只在 entry 里, 删除重添加后读不到 → 重复授信
    if entry.data.get(CONF_USERNAME):
        hass.async_create_task(
            _persist_terminal_fingerprint(
                hass, str(entry.data[CONF_USERNAME]), coordinator.api.terminal_id
            )
        )

    # MQTT 实时通道: 后台连接, 断线自动重试; 失败不影响轮询
    await coordinator.async_start_mqtt()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    await async_setup_services(hass)

    return True


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """条目"重新加载"(HA 三点菜单 Reload) → 卸载后用持久化会话重建.

    会话/令牌/终端标识均持久化在 entry 与安装存储中 —— 重载走 EVERGREEN
    自主续期链,**不需要重新登录/短信授权**;设备全部数据(属性/电量/开门
    记录/告警/型号固件/MQTT 通道)在 setup 内自动重建。
    """
    await hass.config_entries.async_reload(entry.entry_id)


async def _persist_terminal_fingerprint(
    hass: HomeAssistant, username: str, terminal_id: str
) -> None:
    """启动时把 entry 的(已授信)终端指纹同步到安装级存储(幂等, 尽力而为).

    ★ force=True: entry.data/options 里的 tid 就是运行时正在成功使用的
      授权终端(最高可信) —— 必须覆盖 stale 旧指纹, 防止删重装后回退
      到旧指纹再 +1。
    """
    try:
        from .terminal_store import save_terminal_id

        await save_terminal_id(hass, username, terminal_id, force=True)
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Terminal fingerprint persistence skipped", exc_info=True)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the config entry."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    coordinator = domain_data.get(entry.entry_id)
    if coordinator:
        await coordinator.async_shutdown()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        domain_data.pop(entry.entry_id, None)

    return unload_ok
