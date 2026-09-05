"""安装级持久终端标识(与乐橙 App「终端管理」授权绑定).

终端授权按 UA terminalId 记忆。若每次配置流程随机生成新 terminal_id,
即使本 HA 环境早已在终端管理列表里, 重添加/重装后依然会被 12112 拦截,
要求重新短信授权 —— 终端身份必须是**安装级持久**的(对齐手机: 装一次
App = 一个恒定终端), 已授权即永久免短信。
"""

from __future__ import annotations

import logging
import uuid

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import CONF_USERNAME, DOMAIN

_LOGGER = logging.getLogger(__name__)

STORE_KEY = f"{DOMAIN}.terminal_ids"
STORE_VERSION = 1


def _store(hass: HomeAssistant) -> Store:
    return Store(hass, STORE_VERSION, STORE_KEY)


async def get_or_create_terminal_id(hass: HomeAssistant, username: str) -> str:
    """同账号恒定终端标识: 既有 entry(同账号) > 安装存储 > 新生成.

    ① 同账号既有配置项优先 —— 老安装迁移零成本(授权记录就在那个终端上);
    ② 安装级存储(HA .storage) —— 删除集成重添加后仍复用同一终端;
    ③ 都没有才新生成(首次添加, 必然要走一次短信授权, 属预期)。
    """
    account = str(username or "").strip()

    # ① 同账号既有 entry(异常环境/测试桩下不阻断)
    try:
        for entry in hass.config_entries.async_entries(DOMAIN):
            edata = entry.data or {}
            if str(edata.get(CONF_USERNAME) or "").strip() != account:
                continue
            tid = str(
                edata.get("terminal_id")
                or (entry.options or {}).get("terminal_id")
                or ""
            )
            if tid:
                # 指纹此前只存在 entry 里 → 同步落盘,
                # 删除重添加后仍能读到同一已授信终端
                try:
                    data = await _store(hass).async_load() or {}
                except Exception:  # noqa: BLE001
                    data = {}
                terminals = data.get("terminals") or {}
                if not terminals.get(account):
                    terminals[account] = tid
                    try:
                        await _store(hass).async_save({"terminals": terminals})
                    except Exception:  # noqa: BLE001
                        _LOGGER.exception("Persisting terminal_id failed")
                return tid
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Scanning existing entries for terminal_id failed", exc_info=True)

    # ② 安装级存储
    store = _store(hass)
    try:
        data = await store.async_load() or {}
    except Exception:  # noqa: BLE001
        data = {}
    terminals = data.get("terminals") or {}
    tid = str(terminals.get(account) or "")
    if not tid:
        tid = uuid.uuid4().hex
        terminals[account] = tid
        try:
            await store.async_save({"terminals": terminals})
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Persisting terminal_id failed")
    return tid


async def save_terminal_id(
    hass: HomeAssistant, username: str, terminal_id: str,
    force: bool = False,
) -> None:
    """登录/授权成功后把终端指纹写入本地存储文件(下次直接调用, 不再重新生成).

    写入策略: 仅当该账号尚无指纹时写入(保留首个已授信终端);
    ★ force=True(用户亲自完成短信授权/条目内已授信指纹同步)时覆盖 ——
      否则 stale 旧指纹会永远阻塞新授权指纹落盘, 删除重装后再次
      生成新终端 → 终端管理 +1 循环。
    显式重置请用 reset_terminal_id。
    """
    account = str(username or "").strip()
    tid = str(terminal_id or "").strip()
    if not account or not tid:
        return
    store = _store(hass)
    try:
        data = await store.async_load() or {}
    except Exception:  # noqa: BLE001
        data = {}
    terminals = data.get("terminals") or {}
    if terminals.get(account) == tid:
        return
    if terminals.get(account) and not force:
        _LOGGER.warning(
            "Terminal fingerprint for %s already stored (%s…); keeping it. "
            "New value %s… ignored — delete the integration AND reset storage "
            "to re-fingerprint.",
            account, str(terminals[account])[:8], tid[:8],
        )
        return
    if terminals.get(account) and force:
        _LOGGER.info(
            "Terminal fingerprint for %s replaced: %s… → %s… "
            "(user completed authorization)",
            account, str(terminals[account])[:8], tid[:8],
        )
    terminals[account] = tid
    try:
        await store.async_save({"terminals": terminals})
        _LOGGER.info("Terminal fingerprint saved for %s (%s…)", account, tid[:8])
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Persisting terminal fingerprint failed")


async def reset_terminal_id(hass: HomeAssistant, username: str) -> None:
    """清除账号指纹(显式重置: 换账号/重新授信场景)."""
    account = str(username or "").strip()
    if not account:
        return
    store = _store(hass)
    try:
        data = await store.async_load() or {}
    except Exception:  # noqa: BLE001
        data = {}
    terminals = data.get("terminals") or {}
    if account in terminals:
        terminals.pop(account, None)
        try:
            await store.async_save({"terminals": terminals})
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Resetting terminal fingerprint failed")
