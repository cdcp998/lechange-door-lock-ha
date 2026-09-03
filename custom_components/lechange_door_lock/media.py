"""云端媒体管理: 门外截图(节流+缓存) 与 告警图解码。

电池设备约束(WI-007 运维知识):
  - 取流请求(GetRealTransferStreamUrl)自带唤醒语义(~5s 上线, 窗口 30-60s);
  - 唤醒泵(>2 次/30s)触发设备保护 → 快照必须节流(默认 60s, 可配置);
  - 中继等设备仅 ~10s → 休眠唤醒后先等再 PLAY;
  - 并发取流串行化(asyncio.Lock)。
"""

from __future__ import annotations

import asyncio
import logging
import time

from .const import (
    CHANNELS_DUAL,
    CONF_CHANNEL_HOSTS,
    CONF_DEVICE_PASSWORD,
    CONF_SECURITY_CODE,
    CONF_SNAPSHOT_CHANNELS,
    CONF_SNAPSHOT_LAYOUT,
    CONF_SNAPSHOT_MIN_INTERVAL,
    CONF_SNAPSHOT_OSD,
    CONF_SNAPSHOT_OSD_ALPHA,
    CONF_SNAPSHOT_STREAM_ID,
    DEFAULT_SNAPSHOT_CHANNELS,
    DEFAULT_SNAPSHOT_LAYOUT,
    DEFAULT_SNAPSHOT_MIN_INTERVAL,
    DEFAULT_SNAPSHOT_OSD,
    DEFAULT_SNAPSHOT_OSD_ALPHA,
    DEFAULT_SNAPSHOT_STREAM_ID,
    LAYOUT_HSTACK,
    LAYOUT_SINGLE,
    LAYOUT_VSTACK,
)
from .dav_codec import derive_key as dav_derive_key, media_bytes_to_jpeg
from .rtsv import (
    RtsvStreamSession,
    StreamError,
    async_h264_snapshot,
    async_osd_h264,
    derive_stream_key,
    hstack_jpegs,
    overlay_osd,
    vstack_jpegs,
)
from .streams import parse_channel_hosts

_LOGGER = logging.getLogger(__name__)

WAKE_SETTLE_SECONDS = 6.0     # 取流请求唤醒后等待设备上线的兜底时长
COLLECT_SECONDS = 6.0         # 采流时长(IDR 15fps 实测 ~2s 内必到)


def _derive_in_executor(device_id: str, password: str) -> bytes:
    return derive_stream_key(device_id, password)


def _tag_frames(frames: list[bytes], labels: list[str]) -> list[bytes]:
    """逐帧画通道名(右下角, 白字黑描边小字号); 顺序与拼接一致。"""
    return [
        overlay_osd(frame, [label], position="bottom-right") if label else frame
        for frame, label in zip(frames, labels)
    ]


def _compose_osd_frames(
    frames: list[bytes], labels: list[str], layout: str, timestamp: str
) -> bytes:
    """OSD 合成(用户认可的简洁版): 逐帧右下角白字标签 → 拼接 → 整图左上时间戳。

    通道名小而低调(白字 + 1px 黑描边), 置于各通道画面右下角;
    时间戳白字黑描边置于整图左上角, 互不遮挡。
    """
    # 1) 逐帧右下角画通道名(白字黑描边小字号)
    tagged = _tag_frames(frames, labels)
    # 2) 拼接
    if len(tagged) == 1 or layout == LAYOUT_SINGLE:
        combined = tagged[0]
    else:
        combine = vstack_jpegs if layout == LAYOUT_VSTACK else hstack_jpegs
        combined = combine(tagged) or tagged[0]
    # 3) 整图左上角时间戳
    return overlay_osd(combined, [timestamp])


class MediaManager:
    """Per-device media facade (coordinator 持有)。"""

    def __init__(self, coordinator):
        self.coordinator = coordinator
        self._snapshot_lock = asyncio.Lock()
        self._last_snapshot: tuple[float, bytes] = (0.0, b"")
        self._stream_key: bytes | None = None
        self._dav_key: bytes | None = None

    # ---- 凭据(options 覆盖 entry.data;设备密码留空回退安全码) --------------
    def _cred(self, key: str) -> str:
        entry = self.coordinator.entry
        opts = entry.options or {}
        data = entry.data or {}
        return str(opts.get(key) or data.get(key) or "").strip()

    @property
    def security_code(self) -> str:
        """安全码(出厂代) — 告警图解密 + WSSE 回退。"""
        return self._cred(CONF_SECURITY_CODE)

    @property
    def device_password(self) -> str:
        """设备密码(当前代) — 流帧解密;未配置时回退安全码。"""
        return self._cred(CONF_DEVICE_PASSWORD) or self.security_code

    @property
    def snapshot_min_interval(self) -> int:
        opts = self.coordinator.entry.options or {}
        try:
            return max(15, int(opts.get(CONF_SNAPSHOT_MIN_INTERVAL, DEFAULT_SNAPSHOT_MIN_INTERVAL)))
        except (TypeError, ValueError):
            return DEFAULT_SNAPSHOT_MIN_INTERVAL

    @property
    def snapshot_stream_id(self) -> str:
        """取流码流偏好: '1'主码流(默认) / '2'子码流(用户要求时)。

        ★ WI-007 实测: 仅主码流在中继有数据, 子码流 SDP 后零包;
        故选子码流但采流为空时自动回退主码流一次。
        """
        opts = self.coordinator.entry.options or {}
        val = str(opts.get(CONF_SNAPSHOT_STREAM_ID, DEFAULT_SNAPSHOT_STREAM_ID)).strip()
        return val if val in ("1", "2") else DEFAULT_SNAPSHOT_STREAM_ID

    @property
    def snapshot_osd(self) -> bool:
        """门外截图 OSD(时间戳+通道名)开关; 默认 False=干净截图(要才开)。"""
        opts = self.coordinator.entry.options or {}
        return bool(opts.get(CONF_SNAPSHOT_OSD, DEFAULT_SNAPSHOT_OSD))

    @property
    def snapshot_osd_alpha(self) -> int:
        """OSD 底色不透明度(0-255, 默认 160 半透明)。"""
        opts = self.coordinator.entry.options or {}
        try:
            return max(0, min(255, int(opts.get(CONF_SNAPSHOT_OSD_ALPHA, DEFAULT_SNAPSHOT_OSD_ALPHA))))
        except (TypeError, ValueError):
            return DEFAULT_SNAPSHOT_OSD_ALPHA

    # ---- 实时预览 -----------------------------------------------------------
    @property
    def stream_preview_osd(self) -> bool:
        """实时预览 OSD 开关(独立于门外截图; 默认开, 可设置不添加)。"""
        opts = self.coordinator.entry.options or {}
        return bool(opts.get(CONF_STREAM_PREVIEW_OSD, DEFAULT_STREAM_PREVIEW_OSD))

    @property
    def stream_preview_seconds(self) -> int:
        """实时预览录制时长(秒)。"""
        opts = self.coordinator.entry.options or {}
        try:
            return max(3, min(60, int(opts.get(CONF_STREAM_PREVIEW_SECONDS, DEFAULT_STREAM_PREVIEW_SECONDS))))
        except (TypeError, ValueError):
            return DEFAULT_STREAM_PREVIEW_SECONDS

    @property
    def snapshot_layout(self) -> str:
        """多通道布局: hstack 左右 / vstack 上下 / single 单摄单图。"""
        opts = self.coordinator.entry.options or {}
        val = str(opts.get(CONF_SNAPSHOT_LAYOUT, DEFAULT_SNAPSHOT_LAYOUT)).strip()
        return val if val in (LAYOUT_HSTACK, LAYOUT_VSTACK, LAYOUT_SINGLE) else DEFAULT_SNAPSHOT_LAYOUT

    @property
    def snapshot_channels(self) -> list[str]:
        """本次截图要截取的通道(按用户选择, 交集设备实际通道)。

        '0+1' 双摄(默认) / '0' 仅主摄(猫眼) / '1' 仅辅摄。
        """
        opts = self.coordinator.entry.options or {}
        raw = str(opts.get(CONF_SNAPSHOT_CHANNELS, DEFAULT_SNAPSHOT_CHANNELS)).strip()
        available = self._available_channels()
        want = []
        if raw == CHANNELS_DUAL:
            want = ["0", "1"]
        elif raw:
            want = [c.strip() for c in raw.split("+") if c.strip()]
        else:
            want = ["0", "1"]
        picked = [c for c in want if c in available]
        return picked or [available[0]]

    def _available_channels(self) -> list[str]:
        """设备实际摄像头通道列表;空则回退 ['0']。"""
        available = [
            str(ch.get("channelId"))
            for ch in (self.coordinator.data or {}).get("channels") or []
            if isinstance(ch, dict) and str(ch.get("channelId", "")).strip()
        ]
        return available or ["0"]

    # ---- 本地通道地址(局域网直连) ------------------------------------------
    @property
    def channel_hosts(self) -> dict[str, str]:
        """每通道局域网地址 {'0': '192.168.1.10:80', ...}; 空=走云端。"""
        opts = self.coordinator.entry.options or {}
        return parse_channel_hosts(opts.get(CONF_CHANNEL_HOSTS) or {})

    def channel_local_addr(self, channel_id: str) -> tuple[str, int] | None:
        """通道本地 (host, port); 未配置/非 LAN 返回 None。"""
        from .streams import split_host_port, is_lan_host

        raw = self.channel_hosts.get(str(channel_id), "")
        host, port = split_host_port(raw, 80)
        if not host or not is_lan_host(host):
            return None
        return host, port

    async def _capture_local(self, channel_id: str) -> bytes | None:
        """局域网 CGI 快照(优先, 无唤醒/免税流功耗); 失败返回 None。"""
        addr = self.channel_local_addr(channel_id)
        if not addr:
            return None
        host, port = addr
        url = (
            f"http://{host}:{port}/cgi-bin/snapshot.cgi"
            f"?channel={int(channel_id) + 1}&subtype=0"
        )
        try:
            resp = await self.coordinator.api._session.get(url, timeout=5)
            if resp.status == 200:
                data = await resp.read()
                if data[:2] == b"\xff\xd8":
                    return data
                return await self._decode_local_media(data)
            _LOGGER.debug("通道%s LAN 快照 HTTP %s", channel_id, resp.status)
        except Exception as err:  # noqa: BLE001 - 局域网不通属常态(设备不在本网)
            _LOGGER.debug("通道%s LAN 快照失败: %s", channel_id, err)
        return None

    async def _decode_local_media(self, data: bytes) -> bytes | None:
        """本地抓拍若是 DHAV 加密容器(部分固件)则按安全码解。"""
        try:
            return await self.async_alarm_jpeg_bytes(data)
        except Exception:  # noqa: BLE001
            return None

    async def async_alarm_jpeg_bytes(self, raw: bytes) -> bytes | None:
        """原始 DAV/JPEG 字节 → JPEG(供 LAN 快照/告警图共用)。"""
        dav_key = await self.async_dav_key()
        if dav_key is None:
            _LOGGER.warning("媒体解码跳过: 未配置安全码")
            return None
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                None, media_bytes_to_jpeg, raw, self.coordinator.device_id, self.security_code
            )
        except ValueError as err:
            _LOGGER.warning("媒体解码失败: %s", err)
            return None

    async def async_alarm_jpeg(self, pic_url: str) -> bytes | None:
        """下载告警图(picUrl)并解码 → JPEG。"""
        try:
            raw = await self.coordinator.api.async_download_alarm_image(pic_url)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("告警图下载失败: %s", err)
            return None
        dav_key = await self.async_dav_key()
        if dav_key is None:
            _LOGGER.warning("告警图解码跳过: 未配置安全码")
            return None
        try:
            return await self.async_alarm_jpeg_bytes(raw)
        except ValueError as err:
            _LOGGER.warning("告警图解码失败: %s", err)
            return None

    def _channel_label(self, channel_id: str) -> str:
        """通道显示名: 用户命名(channel_names / channels[].channelName) →
        无中文字体回退 ASCII → CHx。

        供截图 OSD 与预览 OSD 共用(同步同一命名规则)。
        """
        data = self.coordinator.data or {}
        label = ""
        # 1) coordinator 属性 channel_names(若已解析)
        ch_names = data.get("channel_names") or {}
        if ch_names:
            raw = ch_names.get(channel_id) or ""
            label = str(raw) if str(raw).strip() else ""
        # 2) 设备 channels[] 自带的 channelName(设备信息原始字段)
        if not label:
            for ch in data.get("channels") or []:
                if isinstance(ch, dict) and str(ch.get("channelId")) == str(channel_id):
                    raw = ch.get("channelName") or ""
                    label = str(raw) if str(raw).strip() else ""
                    break
        if label and any("\u4e00" <= c <= "\u9fff" for c in label):
            from .rtsv import _find_cjk_font

            if _find_cjk_font() is None:
                label = ""
        return label or ("CH" + str(channel_id))

    # ---- KDF(懒派生, executor 执行 PBKDF2 20000 轮) ------------------------
    async def async_stream_key(self) -> bytes | None:
        if not self.device_password:
            return None
        if self._stream_key is None:
            loop = asyncio.get_running_loop()
            self._stream_key = await loop.run_in_executor(
                None, _derive_in_executor, self.coordinator.device_id, self.device_password
            )
        return self._stream_key

    async def async_dav_key(self) -> bytes | None:
        if not self.security_code:
            return None
        if self._dav_key is None:
            loop = asyncio.get_running_loop()
            self._dav_key = await loop.run_in_executor(
                None, dav_derive_key, self.coordinator.device_id, self.security_code
            )
        return self._dav_key

    # ---- 门外截图 -----------------------------------------------------------
    def _camera_channel_ids(self) -> list[str]:
        """本次截图通道列表(组合布局=多通道; single=单通道)。"""
        picked = self.snapshot_channels
        if self.snapshot_layout == LAYOUT_SINGLE:
            return picked[:1]
        return picked

    async def async_cloud_snapshot(
        self,
        force: bool = False,
        want_channels: list[str] | None = None,
        want_layout: str | None = None,
        want_osd: bool | None = None,
    ) -> bytes | None:
        """云端门外截图(RTSV1 采流 → ffmpeg 抽帧), 节流 + 缓存。

        双摄门锁: 所选通道逐一采流后按布局拼接(通道0|通道1);
        单通道/某通道失败: 优雅回退输出已有画面。
        force=True(服务调用)绕过节流; camera entity 轮询走节流。
        want_* 为按次覆盖(自动化/服务按次选择), None=用配置。
        失败返回 None(不抛出, 由调用方记录)。
        """
        now = time.monotonic()
        ts, cached = self._last_snapshot
        if not force and cached and now - ts < self.snapshot_min_interval:
            return cached
        async with self._snapshot_lock:
            # 双检:等锁期间别人可能刚刷过
            now = time.monotonic()
            ts, cached = self._last_snapshot
            if not force and cached and now - ts < self.snapshot_min_interval:
                return cached
            jpeg = await self._do_cloud_snapshot(
                want_channels=want_channels,
                want_layout=want_layout,
                want_osd=want_osd,
            )
            if jpeg:
                self._last_snapshot = (time.monotonic(), jpeg)
            return jpeg

    async def async_record_preview(
        self,
        seconds: float | None = None,
        with_osd: bool | None = None,
        channel_id: str = "0",
    ) -> tuple[bytes | None, str]:
        """实时预览: 采集 N 秒视频 → 可选 OSD 烧录 → 返回 (视频字节, codec)。

        with_osd=None → 用配置(stream_preview_osd, 默认开);
        codec: 'h264'/'h265'(HEVC 优先保持原编码重编码)。
        与门外截图共用节流锁(_snapshot_lock)避免并发唤醒。
        """
        if seconds is None:
            seconds = self.stream_preview_seconds
        osd_on = self.stream_preview_osd if with_osd is None else bool(with_osd)
        stream_key = await self.async_stream_key()
        if not stream_key:
            _LOGGER.warning("实时预览跳过: 未配置安全码/设备密码")
            return None, "h264"
        async with self._snapshot_lock:
            coord = self.coordinator
            was_sleeping = bool((coord.data or {}).get("sleeping"))

            async def _get_url() -> str:
                return await coord.api.async_get_transfer_stream_url(
                    coord.device_id, coord.product_id, channel_id, stream_id="1"
                )

            try:
                if was_sleeping:
                    await _get_url()
                    await asyncio.sleep(WAKE_SETTLE_SECONDS)
                session = RtsvStreamSession(_get_url, coord.device_id, self.device_password)
                video, codec = await session.async_collect(
                    seconds, stream_key
                )
                if not video:
                    _LOGGER.warning("实时预览采流为空")
                    return None, codec
                ch_names = (coord.data or {}).get("channel_names") or {}
                # 通道名: 与截图共用 _channel_label(用户命名/字体回退/ASCII);
                # 时间戳由 rtsv 逐秒叠加真实时间(enable 区间, 预估时长 seconds)
                label = self._channel_label(channel_id)
                texts = [label]
                out = await async_osd_h264(
                    video, texts, overlay=osd_on, codec=codec, seconds=seconds
                )
                return out or video, codec
            except (StreamError, asyncio.TimeoutError, TimeoutError) as err:
                _LOGGER.warning("实时预览失败: %s", err)
                return None, "h264"
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("实时预览异常: %s", err)
                return None, "h264"

    async def _capture_channel(
        self, channel_id: str, stream_key: bytes, wake_first: bool,
        stream_id: str | None = None,
    ) -> tuple[bytes | None, bool]:
        """单通道采流+抽帧; 返回 (jpeg, fell_back_main)。

        wake_first=先发取流请求再等设备上线(休眠唤醒);
        所选码流为空时自动回退主码流('1')重试一次(子码流中继无数据, WI-007)。
        """
        coord = self.coordinator
        sid = stream_id or self.snapshot_stream_id

        async def _get_url() -> str:
            return await coord.api.async_get_transfer_stream_url(
                coord.device_id, coord.product_id, channel_id, stream_id=sid
            )

        try:
            if wake_first:
                await _get_url()  # 兼具唤醒语义(~5s 上线)
                await asyncio.sleep(WAKE_SETTLE_SECONDS)
            session = RtsvStreamSession(_get_url, coord.device_id, self.device_password)
            video, codec = await session.async_collect(COLLECT_SECONDS, stream_key)
            if not video and sid != "1":
                # 子码流零包 → 回退主码流(默认码流)
                _LOGGER.info("通道%s 码流%s 无数据, 回退主码流", channel_id, sid)

                async def _get_url_main() -> str:
                    return await coord.api.async_get_transfer_stream_url(
                        coord.device_id, coord.product_id, channel_id, stream_id="1"
                    )

                session = RtsvStreamSession(_get_url_main, coord.device_id, self.device_password)
                video, codec = await session.async_collect(COLLECT_SECONDS, stream_key)
                sid = "1"
            if not video:
                _LOGGER.warning("通道%s 采流为空", channel_id)
                return None, sid != "1"
            jpeg = await async_h264_snapshot(video, codec=codec)
            if not jpeg:
                _LOGGER.warning("通道%s ffmpeg 抽帧失败(%dB 流)", channel_id, len(video))
            return jpeg, sid != "1"
        except (StreamError, asyncio.TimeoutError, TimeoutError) as err:
            _LOGGER.warning("通道%s 云端采流失败: %s", channel_id, err)
            return None, False
        except Exception as err:  # noqa: BLE001 - 媒体失败不能拖垮轮询
            _LOGGER.warning("通道%s 云端采流异常: %s", channel_id, err)
            return None, False

    async def _do_cloud_snapshot(
        self,
        want_channels: list[str] | None = None,
        want_layout: str | None = None,
        want_osd: bool | None = None,
    ) -> bytes | None:
        """执行一次云端截图(内部, 支持按次参数覆盖)。

        want_channels: 覆盖通道选择('0'/'1'/'0+1'); want_layout/want_osd 同理;
        None → 使用 options 中的持久配置。节流/缓存由 async_cloud_snapshot 负责。
        """
        coord = self.coordinator
        stream_key = await self.async_stream_key()
        if not stream_key:
            _LOGGER.warning("云门外截图跳过: 未配置安全码/设备密码")
            return None
        was_sleeping = bool((coord.data or {}).get("sleeping"))
        if want_channels is not None:
            channel_ids = [c for c in want_channels if c in self._available_channels()]
            channel_ids = channel_ids or self._available_channels()[:1]
        else:
            channel_ids = self._camera_channel_ids()
        layout = want_layout or self.snapshot_layout
        osd_on = self.snapshot_osd if want_osd is None else want_osd
        if len(channel_ids) > 1:
            _LOGGER.debug("云端门外截图: 通道 %s 布局 %s", channel_ids, layout)

        frames: list[bytes] = []
        for idx, ch in enumerate(channel_ids[:4]):  # 安全上限 4 通道
            # LAN 优先(配置了本地地址才试; 设备不在本网则快速失败→云端)
            jpeg = await self._capture_local(ch)
            if not jpeg:
                jpeg, _fell_back = await self._capture_channel(
                    ch, stream_key, wake_first=was_sleeping and idx == 0
                )
            if jpeg:
                frames.append(jpeg)
        if not frames:
            return None
        # OSD 合成(背景全透明, 白字+描边; 无遮画面黑底框):
        #   拼接后统一绘制: 时间戳整图左上 + 通道名各分区右下。
        # 这样 CH0 标签在左画面、CH1 标签在右画面(上下布局同理), 对应各通道位置。
        osd_on = self.snapshot_osd if want_osd is None else (bool(want_osd))
        if osd_on:
            from datetime import datetime

            ch_names = (coord.data or {}).get("channel_names") or {}
            labels = []
            for ch in channel_ids[: len(frames)]:
                if len(frames) == 1:
                    labels.append(ch_names.get(ch) or "CH" + ch)
                else:
                    labels.append(("CH%s %s" % (ch, ch_names.get(ch) or "")).strip())
            # 同步截图/预览命名: 统一 _channel_label(用户命名/字体回退)
            labels = [self._channel_label(ch) for ch in channel_ids[: len(frames)]]
            loop = asyncio.get_running_loop()
            combined = await loop.run_in_executor(
                None,
                _compose_osd_frames,
                frames,
                labels,
                layout,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
        else:
            loop = asyncio.get_running_loop()
            combined = await loop.run_in_executor(None, _combine_no_osd, frames, layout)
        return combined


def _combine_no_osd(frames: list[bytes], layout: str) -> bytes:
    """无 OSD 纯拼接(同步, executor 执行)。"""
    if len(frames) == 1 or layout == LAYOUT_SINGLE:
        return frames[0]
    combine = vstack_jpegs if layout == LAYOUT_VSTACK else hstack_jpegs
    return combine(frames) or frames[0]
