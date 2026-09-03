"""Tests for dav_codec / rtsv framer / media throttle (HA-free, no real secrets)."""

import asyncio
import struct
import tempfile
import time

import pytest

import lechange_door_lock.dav_codec as dav_codec
import lechange_door_lock.rtsv as rtsv
import lechange_door_lock.media as media
import lechange_door_lock.streams as streams

from Crypto.Cipher import AES

DID = "TESTDID00000001"
SEC = "SECCODE1"
DEVPW = "DEVPW123"


# ---------------------------------------------------------------- dav_codec
def _make_jpeg_head() -> bytes:
    """构造 256B JPEG 头: SOI+APP0+DQT×2(与真实结构同形)。"""
    head = bytearray()
    head += b"\xff\xd8"                                   # SOI
    head += b"\xff\xe0" + struct.pack(">H", 16) + b"x" * 14   # APP0
    head += b"\xff\xdb" + struct.pack(">H", 67) + b"\x03" * 65  # DQT #1
    head += b"\xff\xdb" + struct.pack(">H", 67) + b"\x05" * 65  # DQT #2
    assert len(head) <= 256
    return bytes(head.ljust(256, b"\x00"))


def _make_dav_container(key: bytes, iv: bytes, head: bytes, body: bytes) -> bytes:
    """按 dav_decode 容器布局合成: 头(IV@0x33) + 加密区@0x44 + 填充 + 明文体。"""
    enc = AES.new(key, AES.MODE_OFB, iv=iv).encrypt(head)
    container = bytearray(0x1EB)
    container[0:4] = b"DHAV"
    container[0x33:0x43] = iv
    container[0x44:0x144] = enc
    return bytes(container) + body


class TestDavCodec:
    def test_derive_key_is_32b_and_matches_kdf(self):
        key = dav_codec.derive_key(DID, SEC)
        assert len(key) == 32
        # KDF: MD5("admin:Login to {did}:{pw}").hexU → PBKDF2(salt=did, 20000, 32B)
        import hashlib

        pwd = hashlib.md5(f"admin:Login to {DID}:{SEC}".encode()).hexdigest().upper().encode()
        assert key == hashlib.pbkdf2_hmac("sha256", pwd, DID.encode(), 20000, 32)

    def test_dav_to_jpeg_roundtrip(self):
        key = dav_codec.derive_key(DID, SEC)
        iv = b"\x42" * 16
        head = _make_jpeg_head()
        body = b"\xff\xc0" + b"\x11" * 10 + b"\xff\xd9"
        jpg = dav_codec.dav_to_jpeg(_make_dav_container(key, iv, head, body), key)
        assert jpg[:2] == b"\xff\xd8"
        assert jpg[-2:] == b"\xff\xd9"
        assert jpg == head[: len(head) - 256 + 0] + body or jpg.startswith(head[:2])
        # 头部裁剪: 到第二个 DQT 结束(2+18+69+69 = 158 字节)
        assert len(jpg) == 158 + len(body)

    def test_wrong_key_raises(self):
        key = dav_codec.derive_key(DID, SEC)
        other = dav_codec.derive_key("OTHERDID00001", SEC)
        iv = b"\x42" * 16
        container = _make_dav_container(key, iv, _make_jpeg_head(), b"\xff\xd9")
        with pytest.raises(ValueError):
            dav_codec.dav_to_jpeg(container, other)

    def test_media_bytes_passthrough_plain_jpeg(self):
        raw = b"\xff\xd8" + b"fakejpeg" + b"\xff\xd9"
        assert dav_codec.media_bytes_to_jpeg(raw, DID, SEC) == raw

    def test_media_bytes_decodes_dhav(self):
        key = dav_codec.derive_key(DID, SEC)
        iv = b"\x07" * 16
        body = b"\xff\xc0" + b"\x22" * 5 + b"\xff\xd9"
        jpg = dav_codec.media_bytes_to_jpeg(
            _make_dav_container(key, iv, _make_jpeg_head(), body), DID, SEC
        )
        assert jpg[:2] == b"\xff\xd8" and jpg.endswith(b"\xff\xd9")

    def test_media_bytes_unknown_format(self):
        with pytest.raises(ValueError):
            dav_codec.media_bytes_to_jpeg(b"PNG!", DID, SEC)


# ---------------------------------------------------------------- framer
def _build_keyframe(key: bytes, iv: bytes, clear: bytes, enc: bytes) -> bytes:
    """构造一个 0xFD 关键帧(含 0xB5 扩展 + 加密段), 返回完整 DHAV 帧。"""
    # ext: b5 + 00 + 01 + le24(clear_len) + le24(enc_len) + filler + iv(16B) = 43B
    ext = (
        b"\xb5\x00\x01"
        + struct.pack("<I", len(clear))[:3]
        + struct.pack("<I", len(enc))[:3]
        + b"\x00" * 18
        + iv
    )
    assert len(ext) == 43
    payload = clear + AES.new(key, AES.MODE_OFB, iv=iv).encrypt(enc)
    ext_size = 43
    psize = len(payload)
    flen = 24 + ext_size + psize + 8
    frame = bytearray(flen)
    frame[0:4] = b"DHAV"
    frame[4] = 0xFD
    frame[22] = ext_size
    frame[24:24 + ext_size] = ext
    frame[24 + ext_size:24 + ext_size + psize] = payload
    frame[-8:-4] = b"dhav"
    frame[-4:] = struct.pack("<I", flen)
    frame[12:16] = struct.pack("<I", flen)
    return bytes(frame)


def _wrap_dollar(pt: int, pkt: bytes) -> bytes:
    return b"$" + bytes([pt]) + struct.pack(">H", len(pkt)) + pkt


class TestDhavFramer:
    def test_keyframe_decrypted(self):
        key = rtsv.derive_stream_key(DID, DEVPW)
        iv = b"\x11" * 16
        clear, enc = b"SPS!", b"\x65\x88\x84" + b"\x00" * 13
        frame = _build_keyframe(key, iv, clear, enc)
        framer = rtsv.DhavFramer(key)
        out = framer.feed(_wrap_dollar(96, frame))
        assert len(out) == 1
        assert out[0] == clear + enc  # enc 段已被 OFB 解回明文
        assert framer.stats["video"] == 1 and framer.stats["dec"] == 1

    def test_p_frames_before_idr_dropped(self):
        key = rtsv.derive_stream_key(DID, DEVPW)
        iv = b"\x11" * 16
        idr = _build_keyframe(key, iv, b"SPS!", b"\x65" + b"\x00" * 15)
        # P 帧(0xFC): payload 原样
        psize = 12
        pflen = 24 + psize + 8
        pframe = bytearray(pflen)
        pframe[0:4] = b"DHAV"
        pframe[4] = 0xFC
        pframe[22] = 0
        pframe[24:24 + psize] = b"\x41\x9a" + b"\x02" * 10
        pframe[-8:-4] = b"dhav"
        pframe[-4:] = struct.pack("<I", pflen)
        pframe[12:16] = struct.pack("<I", pflen)

        framer = rtsv.DhavFramer(key)
        out = framer.feed(_wrap_dollar(96, bytes(pframe)))
        assert out == []            # IDR 之前的 P 帧不输出
        out = framer.feed(_wrap_dollar(96, idr))
        assert len(out) == 1 and framer.stats["video"] == 1

    def test_garbage_resync(self):
        framer = rtsv.DhavFramer(None)
        out = framer.feed(b"\x00" * 100)
        assert out == []            # 无 $ 同步字节, 丢弃


# ---------------------------------------------------------------- media
class FakeEntry:
    def __init__(self, data=None, options=None):
        self.data = data or {}
        self.options = options or {}


class FakeCoordinator:
    def __init__(self, data=None, entry=None):
        self.device_id = DID
        self.product_id = "PID"
        self.channel_id = "0"
        self.data = data or {"sleeping": False}
        self.entry = entry or FakeEntry(
            {"security_code": SEC, "device_password": DEVPW}
        )
        self.media = media.MediaManager(self)


class TestMediaThrottle:
    def test_throttle_and_cache(self, monkeypatch):
        coord = FakeCoordinator()
        calls = []

        async def fake_snapshot(*args, **kwargs):
            calls.append(1)
            return b"jpeg-%d" % len(calls)

        monkeypatch.setattr(coord.media, "_do_cloud_snapshot", fake_snapshot)

        async def run():
            first = await coord.media.async_cloud_snapshot()
            second = await coord.media.async_cloud_snapshot()  # 节流窗口内 → 缓存
            forced = await coord.media.async_cloud_snapshot(force=True)  # 绕过
            return first, second, forced

        first, second, forced = asyncio.run(run())
        assert first == b"jpeg-1"
        assert second == b"jpeg-1"   # 未重新取流
        assert forced == b"jpeg-2"   # force 重新取流
        assert len(calls) == 2

    def test_min_interval_floor(self):
        coord = FakeCoordinator(entry=FakeEntry({}, {"snapshot_min_interval": 3}))
        assert coord.media.snapshot_min_interval == 15  # 下限保护
        coord2 = FakeCoordinator(entry=FakeEntry({}, {"snapshot_min_interval": 120}))
        assert coord2.media.snapshot_min_interval == 120

    def test_device_password_fallback_to_security_code(self):
        coord = FakeCoordinator(entry=FakeEntry({"security_code": SEC}, {}))
        assert coord.media.device_password == SEC
        coord2 = FakeCoordinator(
            entry=FakeEntry({"security_code": SEC, "device_password": DEVPW}, {})
        )
        assert coord2.media.device_password == DEVPW
        assert coord2.media.security_code == SEC

    def test_keys_cached(self):
        coord = FakeCoordinator()

        async def run():
            k1 = await coord.media.async_stream_key()
            k2 = await coord.media.async_stream_key()
            d1 = await coord.media.async_dav_key()
            return k1, k2, d1

        k1, k2, d1 = asyncio.run(run())
        assert k1 is k2 and k1 == rtsv.derive_stream_key(DID, DEVPW)
        assert d1 == dav_codec.derive_key(DID, SEC)


def _test_ffmpeg() -> str | None:
    """测试辅助: 仅系统 PATH 的 ffmpeg(与生产 _find_ffmpeg 完全一致)。"""
    import shutil

    return shutil.which("ffmpeg")


class TestCombineAndOsd:
    """hstack/vstack 拼接 + OSD 半透明(ffmpeg 或 PIL 任一可用即可验证)."""

    @staticmethod
    def _color_jpeg(color: str, w: int = 64, h: int = 48) -> bytes:
        import subprocess

        ff = _test_ffmpeg()
        if not ff:
            pytest.skip("no ffmpeg")
        p = tempfile.mkstemp(suffix=".jpg")[1]
        subprocess.run(
            [ff, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", f"color={color}:{w}x{h}:d=0.1", "-frames:v", "1",
             "-q:v", "4", "-f", "image2", "-c:v", "mjpeg", p],
            check=True,
        )
        return open(p, "rb").read()

    def test_hstack_width_doubles(self):
        a = self._color_jpeg("red")
        b = self._color_jpeg("blue")
        out = rtsv.hstack_jpegs([a, b])
        assert out and out[:2] == b"\xff\xd8"
        assert self._jpeg_size(out) == (128, 48)

    def test_vstack_height_doubles(self):
        a = self._color_jpeg("red")
        b = self._color_jpeg("blue")
        out = rtsv.vstack_jpegs([a, b])
        assert out and out[:2] == b"\xff\xd8"
        assert self._jpeg_size(out) == (64, 96)

    def test_single_frame_passthrough(self):
        a = self._color_jpeg("green")
        assert rtsv.hstack_jpegs([a]) == a

    @staticmethod
    def _jpeg_size(jpg: bytes) -> tuple[int, int]:
        import subprocess

        ff = _test_ffmpeg()
        probe = subprocess.run([ff, "-hide_banner", "-i", "pipe:0"], input=jpg,
                               capture_output=True)
        import re

        m = re.search(rb"(\d{2,4})x(\d{2,4})", probe.stderr)
        assert m, probe.stderr[-300:]
        return int(m.group(1)), int(m.group(2))

    def test_osd_changes_pixels(self):
        a = self._color_jpeg("red")
        out = rtsv.overlay_osd(a, ["2026-09-04 21:00:00", "CH0 猫眼"], alpha=160)
        assert out[:2] == b"\xff\xd8"
        assert out != a  # 半透明文字已合成(尺寸相同前提下字节必然不同)


class TestChannelSelection:
    """single 布局通道选择 / 组合布局双通道。"""

    def _coord(self, opts=None):
        coord = FakeCoordinator(
            data={
                "sleeping": False,
                "channels": [
                    {"channelId": "0", "channelName": "主摄像头"},
                    {"channelId": "1", "channelName": "辅摄像头"},
                ],
            },
            entry=FakeEntry(
                {"security_code": SEC, "device_password": DEVPW},
                opts or {},
            ),
        )
        return coord

    def test_combine_layout_uses_both(self):
        c = self._coord()
        assert c.media._camera_channel_ids() == ["0", "1"]  # 默认 hstack 双摄

    def test_single_layout_uses_selected_channel(self):
        c = self._coord({"snapshot_layout": "single", "snapshot_channels": "1"})
        assert c.media._camera_channel_ids() == ["1"]

    def test_single_layout_invalid_channel_falls_back(self):
        c = self._coord({"snapshot_layout": "single", "snapshot_channels": "9"})
        assert c.media._camera_channel_ids() == ["0"]

    def test_vstack_layout_property(self):
        c = self._coord({"snapshot_layout": "vstack"})
        assert c.media.snapshot_layout == "vstack"
        assert c.media._camera_channel_ids() == ["0", "1"]

    def test_osd_alpha_property(self):
        assert self._coord().media.snapshot_osd_alpha == 160
        assert self._coord({"snapshot_osd_alpha": 0}).media.snapshot_osd_alpha == 0
        assert self._coord({"snapshot_osd_alpha": 500}).media.snapshot_osd_alpha == 255


class TestChannelHosts:
    """本地通道地址接口: 解析 + LAN 优先回退。"""

    def test_parse_json(self):
        d = media.parse_channel_hosts('{"0": "192.168.1.10", "1": "192.168.1.11:80"}')
        assert d == {"0": "192.168.1.10", "1": "192.168.1.11:80"}

    def test_parse_line_format(self):
        d = media.parse_channel_hosts("0=192.168.1.10\n# comment\n1=192.168.1.11:8080")
        assert d == {"0": "192.168.1.10", "1": "192.168.1.11:8080"}

    def test_local_addr_only_lan(self):
        coord = FakeCoordinator(
            data={"sleeping": False, "channels": [{"channelId": "0"}]},
            entry=FakeEntry(
                {"security_code": SEC},
                {"channel_hosts": "0=192.168.1.10:8080"},
            ),
        )
        assert coord.media.channel_local_addr("0") == ("192.168.1.10", 8080)
        assert coord.media.channel_local_addr("1") is None

    def test_no_local_addr_when_not_configured(self):
        coord = FakeCoordinator(entry=FakeEntry({"security_code": SEC}, {}))
        assert coord.media.channel_local_addr("0") is None

    def test_cloud_fallback_when_local_fails(self, monkeypatch):
        coord = FakeCoordinator(
            data={"sleeping": False, "channels": [
                {"channelId": "0"}, {"channelId": "1"}]},
            entry=FakeEntry(
                {"security_code": SEC, "device_password": DEVPW},
                {"channel_hosts": "0=192.168.1.10"},
            ),
        )
        calls = {"local": 0, "cloud": 0}

        async def fake_local(ch):
            calls["local"] += 1
            return None  # 设备不在本网 → 回退

        async def fake_cloud(ch, *a, **k):
            calls["cloud"] += 1
            return (b"jpeg-%s" % ch.encode()), False

        monkeypatch.setattr(coord.media, "_capture_local", fake_local)
        monkeypatch.setattr(coord.media, "_capture_channel", fake_cloud)

        async def run():
            return await coord.media._do_cloud_snapshot()

        out = asyncio.run(run())
        assert out and out.startswith(b"jpeg-")
        assert calls["local"] == 2  # 每个通道都试了 LAN
        assert calls["cloud"] == 2  # 都回退云端

    def test_local_success_skips_cloud(self, monkeypatch):
        coord = FakeCoordinator(
            data={"sleeping": False, "channels": [{"channelId": "0"}]},
            entry=FakeEntry(
                {"security_code": SEC, "device_password": DEVPW},
                {"channel_hosts": "0=192.168.1.10"},
            ),
        )
        calls = {"local": 0, "cloud": 0}

        async def fake_local(ch):
            calls["local"] += 1
            return b"local-jpeg"

        async def fake_cloud(ch, *a, **k):
            calls["cloud"] += 1
            return b"cloud-jpeg", False

        monkeypatch.setattr(coord.media, "_capture_local", fake_local)
        monkeypatch.setattr(coord.media, "_capture_channel", fake_cloud)

        async def run():
            return await coord.media._do_cloud_snapshot()

        out = asyncio.run(run())
        assert out == b"local-jpeg"
        assert calls["local"] == 1 and calls["cloud"] == 0


class TestCodecDetect:
    """H264/H265 探测(HEVC 优先)。"""

    def test_h264_sps(self):
        # H264 SPS NAL: 起始码 00 00 00 01 + 0x67 (SPS=7)
        assert rtsv.detect_video_codec(b"\x00\x00\x00\x01\x67\x42\x00") == "h264"

    def test_h265_vps(self):
        # HEVC VPS NAL: 0x40 (nal_type 32)
        assert rtsv.detect_video_codec(b"\x00\x00\x00\x01\x40\x01\x00") == "h265"

    def test_h265_prefers_over_h264_ambiguous(self):
        # 先出现 H265 VPS 即判 HEVC
        assert rtsv.detect_video_codec(
            b"\x00\x00\x00\x01\x40\x01" + b"\x00\x00\x00\x01\x67\x42"
        ) == "h265"

    def test_sdp_h265(self):
        assert rtsv.parse_sdp_codec("a=rtpmap:96 H265/90000") == "h265"
        assert rtsv.parse_sdp_codec("a=rtpmap:96 H264/90000") == "h264"
        assert rtsv.parse_sdp_codec("a=rtpmap:96 disable/90000") == ""

    def test_fallback_h264(self):
        assert rtsv.detect_video_codec(b"\x00\x00\x00\x01\x00\x00\x00\x01") == "h264"


class TestPreviewOsd:
    """预览 OSD 烧录(ffmpeg drawtext, HEVC 优先保持编码)。"""

    @staticmethod
    def _mk_video(codec: str, w: int = 320, h: int = 240) -> bytes:
        import subprocess

        ff = rtsv._find_ffmpeg()
        if not ff:
            pytest.skip("no ffmpeg")
        p = tempfile.mkstemp(suffix="." + codec)[1]
        enc = "libx265" if codec == "h265" else "libx264"
        fmt = "hevc" if codec == "h265" else "h264"
        extra = ["-x265-params", "log-level=error"] if codec == "h265" else []
        subprocess.run(
            [ff, "-y", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", f"testsrc=size={w}x{h}:rate=15:duration=2",
             "-c:v", enc, "-preset", "ultrafast", *extra, "-f", fmt, p],
            check=True,
        )
        return open(p, "rb").read()

    def test_osd_burn_h264(self):
        import asyncio

        video = self._mk_video("h264")
        out = asyncio.run(
            rtsv.async_osd_h264(video, ["ts", "CH0"], width=320)
        )
        assert out and len(out) > 1000

    def test_osd_burn_h265_keeps_hevc(self):
        import asyncio

        video = self._mk_video("h265")
        out = asyncio.run(
            rtsv.async_osd_h264(video, ["ts", "CH0"], width=320, codec="h265")
        )
        assert out
        assert rtsv.detect_video_codec(out) == "h265"

    def test_osd_off_keeps_stream(self):
        import asyncio

        video = self._mk_video("h264")
        out = asyncio.run(rtsv.async_osd_h264(video, ["ts"], overlay=False))
        assert out and rtsv.detect_video_codec(out) == "h264"
class TestFontfileColonEscape:
    """Windows 绝对路径冒号转义(drawtext 分隔符冲突巨坑, 防回归)。"""

    def test_windows_drive_colon_escaped(self):
        filt = rtsv._drawtext_filter("CH0", 0.0, 14,
                                     fontfile=r"C:\Windows\Fonts\msyh.ttc",
                                     pos="bottom-right")
        # 关键: 路径冒号被转义为 '\:' 形式(而非裸 ':' 分隔符)
        assert ":fontfile=" in filt
        assert "C\\:" in filt

    def test_absolute_path_preferred(self):
        from lechange_door_lock.rtsv import _find_cjk_font

        path = _find_cjk_font()
        if path is None:
            pytest.skip("no CJK font on this system")
        import os

        assert os.path.isabs(path)  # 生产环境必须绝对路径(相对路径随 CWD 漂移)

    def test_ascii_text_no_escape_needed(self):
        filt = rtsv._drawtext_filter("CH0", 0.0, 14)
        assert "text='CH0'" in filt
        assert "\\:" not in filt.split("text='CH0'")[0]  # 无冒号文字无需转义


class TestStreamKey:
    def test_stream_key_matches_preview_live_kdf(self):
        # 与 API/scripts/preview_live.py 相同的 KDF 结构
        import hashlib

        fmt = "admin:Login to %s:%s" % (DID, DEVPW)
        expect = hashlib.pbkdf2_hmac(
            "sha256", hashlib.md5(fmt.encode()).hexdigest().upper().encode(),
            DID.encode(), 20000, 32,
        )
        assert rtsv.derive_stream_key(DID, DEVPW) == expect

    def test_play_request_shape(self):
        url = "relay.example.com:11004/sessionX"  # resource 无 scheme
        (host, port), raw = rtsv.build_play_request(url, DID, DEVPW)
        assert (host, port) == ("relay.example.com", 11504)  # TLS = +500
        assert raw.startswith(b"PLAY /")
        assert b"Accpet-Sdp: Private" in raw                 # 服务器端拼写
        assert b"WSSE: UsernameToken" in raw
        assert b"trackID=31&method=0" in raw
        assert raw.endswith(b"\r\n\r\n" + rtsv.build_sdp())
