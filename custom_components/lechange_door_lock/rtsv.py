"""RTSV1 云端实时流(异步): 取流 → TLS PLAY(WSSE) → RTP/DHAV 解帧 → 解密 → H.264。

协议要点(实测打通, 快照 480×640):
  1. things.media.GetRealTransferStreamUrl → resource(TCP:11004) / tls_resource(+500)
     ★ 该请求兼具唤醒语义(休眠设备 ~5s 上线); 仅主码流 streamId='1' 中继有数据
  2. TLS PLAY (WSSE UsernameToken) → 200 OK + SDP(H264 PT96)
  3. $ 打包 RTP(PT96 载 DHAV) → DHAV 帧重组(0xFC P帧 / 0xFD 关键帧)
  4. 0xFD 帧头 0xB5 扩展: clear(le24)+enc(le24)+IV(16B);
     enc 段(SPS+PPS+IDR 片头) AES-256-OFB(key=KDF(设备密码)) 解密
  5. 输出裸 H.264 Annex-B, ffmpeg 可直接转码/抽帧

密码语义(两套体系): 帧解密用「当前设备密码」;
WSSE 认证实测用安全码/设备密码均可(200 OK)。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
import os
import secrets
import ssl
import subprocess
import tempfile
import time
from datetime import datetime, timedelta

from .const import (
    MEDIA_STREAM_PAYLOAD_TYPES,
    STREAM_CONNECT_TIMEOUT,
    STREAM_KEEPALIVE_INTERVAL,
    STREAM_MAX_FRAME,
)

_LOGGER = logging.getLogger(__name__)

try:
    from Crypto.Cipher import AES
except ImportError:  # pragma: no cover - 缺 pycryptodome 时仅帧解密不可用
    AES = None


def detect_video_codec(annexb: bytes, sdp: str = "") -> str:
    """探测采集流编码: 'h264' / 'h265'(HEVC 优先)。

    三层判定(SDP rtpmap → NALU 类型 → 默认 h264):
      1. SDP 中 a=rtpmap:... H265/HEVC → h265
      2. NALU: HEVC 起始码后 nal_type = (byte>>1)&0x3F; VPS(32)/SPS(33) → h265;
         H.264 nal_type = byte&0x1F; SPS(7)/IDR(5) → h264
      3. 兜底 h264(本机实测主码流为 H.264)
    """
    sdp_l = sdp.lower()
    if any(x in sdp_l for x in ("h265", "hevc", "h.265")):
        return "h265"
    i = 0
    n = len(annexb)
    while i < n - 3:
        if annexb[i:i + 4] == b"\x00\x00\x00\x01" or annexb[i:i + 3] == b"\x00\x00\x01":
            skip = 4 if annexb[i:i + 4] == b"\x00\x00\x00\x01" else 3
            nal = annexb[i + skip] if i + skip < n else 0
            hevc_type = (nal >> 1) & 0x3F
            if hevc_type in (32, 33, 34, 35):  # VPS/SPS/PPS/IDR_W_RADL
                return "h265"
            h264_type = nal & 0x1F
            if h264_type in (7, 8, 5):  # SPS/PPS/IDR
                return "h264"
            i += skip
            continue
        i += 1
    return "h264"


def parse_sdp_codec(sdp: str) -> str:
    """从 SDP 提取视频 codec('h264'/'h265'); 无标记返回 ''。"""
    for line in sdp.splitlines():
        if line.startswith("a=rtpmap:"):
            parts = line.split(" ", 1)
            if len(parts) == 2:
                payload = parts[1].strip().lower()
                if payload.startswith(("h265", "hevc", "h.265")):
                    return "h265"
                if payload.startswith(("h264", "avc")):
                    return "h264"
    return ""


class StreamError(RuntimeError):
    """云端取流失败。"""


USERNAME = "admin"
LOGIN_FMT_TPL = "admin:Login to %s:%s"


def derive_stream_key(device_id: str, device_password: str) -> bytes:
    """设备密码 → 流帧解密密钥(当前代 KDF)。"""
    fmt = LOGIN_FMT_TPL % (device_id, device_password)
    pwd = hashlib.md5(fmt.encode()).hexdigest().upper().encode()
    return hashlib.pbkdf2_hmac("sha256", pwd, device_id.encode(), 20000, 32)


def wsse_token(device_id: str, password: str, nonce: str, created: str) -> str:
    """WSSE UsernameToken 值(密码用设备密码; 实测安全码亦通过 200 OK)。"""
    md5_tok = hashlib.md5((LOGIN_FMT_TPL % (device_id, password)).encode()).hexdigest().upper()
    sha_tok = hashlib.sha256((LOGIN_FMT_TPL % (device_id, password)).encode()).hexdigest().upper()
    digest = base64.b64encode(
        hashlib.sha1((nonce + created + md5_tok).encode()).digest()).decode()
    light = base64.b64encode(
        hashlib.sha256((nonce + created + sha_tok).encode()).digest()).decode()
    return ('UsernameToken Username="admin", PasswordDigest="%s", '
            'LightweightDigest="%s", Nonce="%s", Created="%s"'
            % (digest, light, nonce, created))


def build_sdp() -> bytes:
    """App 同款 5-track SDP。"""
    return (
        "v=0\r\n"
        "o=- 0 0 IN IP4 0.0.0.0\r\n"
        "s=Media Server\r\n"
        "c=IN IP4 0.0.0.0\r\n"
        "t=0 0\r\n"
        "a=control:*\r\n"
        "a=packetization-supported:DH\r\n"
        "a=rtppayload-supported:DH\r\n"
        "a=range:npt=now-\r\n"
        "m=video 0 RTP/AVP 0\r\n"
        "a=control:trackID=0\r\n"
        "a=framerate:0\r\n"
        "a=rtpmap:0 disable/90000\r\n"
        "a=fmtp\r\n"
        "a=sendonly\r\n"
        "m=audio 0 RTP/AVP 0\r\n"
        "a=control:trackID=1\r\n"
        "a=rtpmap:0 disable/8000\r\n"
        "a=sendonly\r\n"
        "m=audio 0 RTP/AVP 0\r\n"
        "a=control:trackID=2\r\n"
        "a=rtpmap:0 disable/8000\r\n"
        "a=sendonly\r\n"
        "m=application 0 RTP/AVP 100\r\n"
        "a=control:trackID=3\r\n"
        "a=rtpmap:100 stream-assist-frame/90000\r\n"
        "a=sendonly\r\n"
        "m=application 0 RTP/AVP 107\r\n"
        "a=control:trackID=4\r\n"
        "a=rtpmap:107 vnd.onvif.metadata/90000\r\n"
        "a=sendonly\r\n"
        "m=audio 0 RTP/AVP 8\r\n"
        "a=control:trackID=5\r\n"
        "a=rtpmap:8 PCMA/16000\r\n"
        "a=sendonly\r\n"
    ).encode()


def build_play_request(
    transfer_url: str, device_id: str, password: str
) -> tuple[tuple[str, int], bytes]:
    """构造 TLS PLAY 请求; 返回 ((host, tls_port), 完整请求字节)。

    TLS 端口 = resource 端口 + 500 (11004→11504, 与 tls_resource 一致)。
    """
    hostport = transfer_url.split("/")[0]
    host, _, port = hostport.partition(":")
    tls_port = int(port) + 500
    target = "/" + transfer_url.split("/", 1)[1]
    target += ("&" if "?" in target else "?") + "trackID=31&method=0"

    nonce = secrets.token_hex(16)
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    wsse = wsse_token(device_id, password, nonce, created)
    sdp = build_sdp()
    req = [
        "PLAY " + target + " HTTP/1.1",
        "Accpet-Sdp: Private",  # 服务器端拼写如此(App 原样)
        'Authorization: WSSE profile="UsernameToken"',
        "Connect-Type: P2P",
        "Connection: keep-alive",
        "Cseq: 0",
        "Host: " + hostport,
        "Private-Length: " + str(len(sdp)),
        "Private-Type: application/sdp",
        "Speed: 1.000000",
        "User-Agent: Http Stream Client/1.0",
        "WSSE: " + wsse,
        "x-pcs-request-id: " + secrets.token_hex(8),
    ]
    return (host, tls_port), "\r\n".join(req).encode() + b"\r\n\r\n" + sdp


def _rtp_payload(pkt: bytes) -> tuple[int, bytes] | None:
    """$ 打包 RTP → (payload_type, payload)。"""
    if len(pkt) < 12 or pkt[0] >> 6 != 2:
        return None
    csrc = pkt[0] & 0x0F
    off = 12 + 4 * csrc
    if pkt[0] & 0x10:  # extension header
        if len(pkt) < off + 4:
            return None
        off += 4 + 4 * int.from_bytes(pkt[off + 2:off + 4], "big")
    end = len(pkt)
    if pkt[0] & 0x20:  # padding
        pad = pkt[-1]
        if not pad or pad > end - off:
            return None
        end -= pad
    if off > end:
        return None
    return pkt[1] & 0x7F, pkt[off:end]


def _b5_info(ext: bytes) -> tuple[int, int, bytes] | None:
    """0xB5 扩展解析: [i]=0xB5, [i+2]=1, clear=le24(i+3), enc=le24(i+6), iv=(i+27..43)。"""
    i = ext.find(b"\xb5")
    if i < 0 or i + 43 > len(ext) or ext[i + 2] != 1:
        return None
    clear = int.from_bytes(ext[i + 3:i + 6], "little")
    enc = int.from_bytes(ext[i + 6:i + 9], "little")
    return clear, enc, ext[i + 27:i + 43]


class DhavFramer:
    """$RTP / 裸 DHAV 混合流 → 帧重组 → 关键帧头解密 → H.264 输出。

    started=False 时丢弃关键帧之前的 P 帧(解码器需要 SPS/PPS/IDR 起播)。
    """

    def __init__(self, frame_key: bytes | None):
        self.key = frame_key
        self.wire = bytearray()
        self.dhav = bytearray()
        self.started = False
        self.stats = {"pkt": 0, "dhav": 0, "video": 0, "key": 0, "dec": 0}

    def feed(self, chunk: bytes) -> list[bytes]:
        """喂入网络字节, 返回解出的 H.264 帧列表。"""
        self.wire.extend(chunk)
        out: list[bytes] = []
        while len(self.wire) >= 4:
            if self.wire[0] != 0x24:  # '$'
                m = self.wire.find(b"$")
                if m < 0:
                    self.wire.clear()
                    break
                del self.wire[:m]
                if len(self.wire) < 4:
                    break
            self.stats["pkt"] += 1
            size = int.from_bytes(self.wire[2:4], "big")
            hs = 4
            if len(self.wire) >= 10 and self.wire[6:10] == b"DHAV":
                esz = int.from_bytes(self.wire[2:6], "big")
                dsz = int.from_bytes(self.wire[18:22], "little") if len(self.wire) >= 22 else 0
                if dsz == esz:
                    size, hs = esz, 6
            elif size == 0:
                size, hs = int.from_bytes(self.wire[2:6], "big"), 6
            if size <= 0 or size > STREAM_MAX_FRAME:
                del self.wire[0]
                continue
            if len(self.wire) < hs + size:
                break
            pkt = bytes(self.wire[hs:hs + size])
            del self.wire[:hs + size]
            if pkt.startswith(b"DHAV"):
                self.dhav.extend(pkt)
            else:
                pl = _rtp_payload(pkt)
                if pl is None or pl[0] not in MEDIA_STREAM_PAYLOAD_TYPES:
                    continue
                self.dhav.extend(pl[1])
            out.extend(self._drain())
        return out

    def _drain(self) -> list[bytes]:
        out: list[bytes] = []
        while True:
            if not self.dhav.startswith(b"DHAV"):
                m = self.dhav.find(b"DHAV")
                if m < 0:
                    if len(self.dhav) > 3:
                        del self.dhav[:-3]
                    break
                del self.dhav[:m]
            if len(self.dhav) < 24:
                break
            flen = int.from_bytes(self.dhav[12:16], "little")
            if flen < 32 or flen > STREAM_MAX_FRAME:
                del self.dhav[:4]
                continue
            if len(self.dhav) < flen:
                break
            frame = bytes(self.dhav[:flen])
            # 页脚校验: ... 'dhav' + le32(flen)
            if frame[-8:-4] != b"dhav" or int.from_bytes(frame[-4:], "little") != flen:
                del self.dhav[:4]
                continue
            del self.dhav[:flen]
            self.stats["dhav"] += 1
            ftype = frame[4]
            if ftype not in (0xFC, 0xFD):  # 只关心视频帧(0xFD=IDR, 0xFC=P)
                continue
            if not self.started:
                if ftype != 0xFD:
                    continue  # 等首个关键帧再起播
                self.started = True
            self.stats["video"] += 1
            ext_size = frame[22]
            poff = 24 + ext_size
            psize = flen - 8 - poff
            if psize < 0:
                continue
            payload = bytearray(frame[poff:poff + psize])
            if ftype == 0xFD and self.key is not None:
                self.stats["key"] += 1
                info = _b5_info(frame[24:poff])
                if info:
                    clear, enc, iv = info
                    end = clear + enc
                    if AES is not None and end <= len(payload):
                        d = AES.new(self.key, AES.MODE_OFB, iv=iv)
                        payload[clear:end] = d.decrypt(bytes(payload[clear:end]))
                        self.stats["dec"] += 1
            out.append(bytes(payload))
        return out


def _insecure_ssl_context() -> ssl.SSLContext:
    """中继 TLS 上下文(与实测脚本一致: 不校验中继证书)。"""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class RtsvStreamSession:
    """一次云端取流会话: GetRealTransferStreamUrl → PLAY → 采帧。"""

    def __init__(
        self,
        async_get_url,           # async () -> str (由 imou_client 侧注入)
        device_id: str,
        password: str,           # WSSE 密码(设备密码或安全码, 实测均可)
    ):
        self._async_get_url = async_get_url
        self.device_id = device_id
        self.password = password

    async def async_get_transfer_url(self) -> str:
        return await self._async_get_url()

    async def async_collect(
        self,
        seconds: float,
        frame_key: bytes | None,
        max_bytes: int = 4 * 1024 * 1024,
        wait_online: float = 0.0,
    ) -> tuple[bytes, str]:
        """连接中继采集 ~seconds 秒, 返回 (H.264/H.265 Annex-B, codec)。

        codec='h264'|'h265'(HEVC 优先, SDP+NALU 双层探测)。
        wait_online>0 时先等设备上线(取流请求自带唤醒, ~5s; 上线窗口 30-60s,
        中继等设备仅 ~10s, 故先 sleep 再 PLAY)。
        """
        if wait_online > 0:
            await asyncio.sleep(wait_online)
        url = await self.async_get_transfer_url()
        hp, req = build_play_request(url, self.device_id, self.password)
        _LOGGER.debug("RTSV1 dial %s (target=%s...)", hp, url[:60])
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(hp[0], hp[1], ssl=_insecure_ssl_context()),
            timeout=STREAM_CONNECT_TIMEOUT,
        )
        try:
            writer.write(req)
            await asyncio.wait_for(writer.drain(), timeout=STREAM_CONNECT_TIMEOUT)

            # ---- 响应头 ----
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = await asyncio.wait_for(
                    reader.read(65536), timeout=STREAM_CONNECT_TIMEOUT
                )
                if not chunk:
                    raise StreamError("closed before response headers")
                buf += chunk
            head, _, rest = buf.partition(b"\r\n\r\n")
            if b" 200 " not in head:
                status = head.split(b"\r\n", 1)[0].decode(errors="replace")
                raise StreamError("PLAY failed: %s" % status)
            # 响应头中可能带 SDP(Private-Type: application/sdp) → 解析 codec
            sdp_codec = ""
            try:
                head_text = head.decode(errors="replace")
                sdp_codec = parse_sdp_codec(head_text)
            except Exception:  # noqa: BLE001
                pass

            # ---- 采集循环(带 OPTIONS keepalive) ----
            framer = DhavFramer(frame_key)
            out = bytearray()
            for fr in framer.feed(rest):
                out.extend(fr)
            deadline = time.monotonic() + seconds
            last_keep = time.monotonic()
            cseq = 1
            target = "/" + url.split("/", 1)[1]
            while time.monotonic() < deadline and len(out) < max_bytes:
                timeout = max(0.05, min(1.0, deadline - time.monotonic()))
                try:
                    chunk = await asyncio.wait_for(reader.read(65536), timeout=timeout)
                except (asyncio.TimeoutError, TimeoutError):
                    chunk = b""
                if chunk:
                    for fr in framer.feed(chunk):
                        out.extend(fr)
                now = time.monotonic()
                if now - last_keep >= STREAM_KEEPALIVE_INTERVAL:
                    keep = (
                        "OPTIONS %s HTTP/1.1\r\nCseq: %d\r\n"
                        "User-Agent: Http Stream Client/1.0\r\n\r\n"
                        % (target, cseq)
                    ).encode()
                    try:
                        writer.write(keep)
                        await asyncio.wait_for(writer.drain(), timeout=5)
                    except (OSError, asyncio.TimeoutError, TimeoutError):
                        break
                    cseq += 1
                    last_keep = now
            _LOGGER.debug(
                "RTSV1 collect done: %dB stats=%s", len(out), framer.stats
            )
            annexb = bytes(out)
            codec = detect_video_codec(annexb, sdp_codec)
            _LOGGER.debug("RTSV1 codec: %s (sdp=%s, %dB)", codec, sdp_codec or "-", len(annexb))
            return annexb, codec
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, asyncio.TimeoutError):  # noqa: BLE001
                pass


def _find_ffmpeg() -> str | None:
    """定位 ffmpeg: 仅系统 PATH(manifest dependencies=['ffmpeg'] 保证安装)。

    HA 集成依赖声明为 `ffmpeg`(系统二进制, 无 imageio 回退);
    若缺失, 快照/预览 OSD 功能不可用并记录明确提示。
    """
    import shutil

    return shutil.which("ffmpeg")


def hstack_jpegs(frames: list[bytes]) -> bytes | None:
    """多张 JPEG 横向拼接为一图(左右布局)。"""
    return _combine_jpegs(frames, "hstack")


def vstack_jpegs(frames: list[bytes]) -> bytes | None:
    """多张 JPEG 纵向拼接为一图(上下布局)。"""
    return _combine_jpegs(frames, "vstack")


def _combine_jpegs(frames: list[bytes], layout: str) -> bytes | None:
    """组合多图: layout='hstack' 左右 / 'vstack' 上下。

    同步函数(调用方放 executor); 优先 ffmpeg, 回退 PIL, 均不可用返回 None。
    对齐方向缩放到最小边(横拼等高/竖拼等宽)。
    """
    if not frames:
        return None
    if len(frames) == 1:
        return frames[0]

    out = _combine_ffmpeg(frames, layout)
    if out:
        return out
    return _combine_pil(frames, layout)


def _combine_ffmpeg(frames: list[bytes], layout: str) -> bytes | None:
    """ffmpeg 拼接: hstack(等高)/vstack(等宽)。"""
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        return None
    n = len(frames)
    if layout == "vstack":
        # 等宽缩放: 'min(iw,iw,...)'
        min_side = "min(" + "\\,".join("iw" for _ in range(n)) + ")"
        scale_fmt = "[%d:v]scale=%s:-2[s%d]"
        stack = "vstack=inputs=%d" % n
    else:
        min_side = "min(" + "\\,".join("ih" for _ in range(n)) + ")"
        scale_fmt = "[%d:v]scale=-2:%s[s%d]"
        stack = "hstack=inputs=%d" % n
    filter_complex = (
        ";".join(scale_fmt % (i, min_side, i) for i in range(n))
        + "".join("[s%d]" % i for i in range(n))
        + stack
    )
    paths: list[str] = []
    try:
        for fr in frames:
            fd, p = tempfile.mkstemp(suffix=".jpg")
            with os.fdopen(fd, "wb") as f:
                f.write(fr)
            paths.append(p)
        cmd: list[str] = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
        for p in paths:
            cmd += ["-i", p]
        cmd += [
            "-filter_complex", filter_complex,
            "-frames:v", "1", "-q:v", "4",
            "-f", "image2", "-c:v", "mjpeg", "pipe:1",
        ]
        proc = subprocess.run(  # noqa: S603
            cmd, capture_output=True, timeout=25
        )
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout
        _LOGGER.debug("combine ffmpeg failed: %s", proc.stderr[-200:] if proc.stderr else "")
        return None
    except (OSError, subprocess.SubprocessError) as err:
        _LOGGER.debug("combine ffmpeg error: %s", err)
        return None
    finally:
        for p in paths:
            try:
                os.unlink(p)
            except OSError:
                pass


def _combine_pil(frames: list[bytes], layout: str) -> bytes | None:
    """PIL 拼接回退(hstack 等高 / vstack 等宽)。"""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        imgs = [Image.open(io.BytesIO(f)) for f in frames]
        if layout == "vstack":
            w = min(im.width for im in imgs)
            resized = []
            for im in imgs:
                if im.width != w:
                    im = im.resize((w, round(im.height * w / im.width)))
                resized.append(im.convert("RGB"))
            total_h = sum(im.height for im in resized)
            canvas = Image.new("RGB", (w, total_h))
            y = 0
            for im in resized:
                canvas.paste(im, (0, y))
                y += im.height
        else:
            h = min(im.height for im in imgs)
            resized = []
            for im in imgs:
                if im.height != h:
                    im = im.resize((round(im.width * h / im.height), h))
                resized.append(im.convert("RGB"))
            total_w = sum(im.width for im in resized)
            canvas = Image.new("RGB", (total_w, h))
            x = 0
            for im in resized:
                canvas.paste(im, (x, 0))
                x += im.width
        buf = io.BytesIO()
        canvas.save(buf, format="JPEG", quality=88)
        return buf.getvalue()
    except Exception:  # noqa: BLE001 - 坏图回退 None
        return None


def overlay_osd_region(jpeg: bytes, text: str, region: tuple[float, float, float, float]) -> bytes:
    """在整图的一个相对区域(0-1 x0,y0,x1,y1)的**右下角**绘制一条文字(白字黑描边)。

    用于多通道组合图: 每个通道对应区域画自己的通道名;
    白色细字(黑 1px 描边)贴合区域右下角, 不喧宾夺主。
    """
    try:
        from PIL import Image, ImageDraw, ImageFont

        img = Image.open(io.BytesIO(jpeg)).convert("RGB")
        x0, y0, x1, y1 = region
        rw = max(1, int((x1 - x0) * img.width))
        rh = max(1, int((y1 - y0) * img.height))
        # 小字号: 按区域自适应, 上限 20px(不再夸大)
        size = max(10, min(rh // 18, rw // 26, 20))
        font = None
        for cand in (
            r"C:\Windows\Fonts\msyh.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/System/Library/Fonts/PingFang.ttc",
        ):
            try:
                if os.path.exists(cand):
                    font = ImageFont.truetype(cand, size)
                    break
            except OSError:
                continue
        if font is None:
            try:
                font = ImageFont.load_default(size=size)
            except TypeError:
                font = ImageFont.load_default()

        draw = ImageDraw.Draw(img)
        try:
            box = draw.textbbox((0, 0), text, font=font)
            tw, th = box[2] - box[0], box[3] - box[1]
        except AttributeError:
            tw, th = draw.textsize(text, font=font)
        # 区域右下角留边(10px 右 / 8px 下, 依区域尺寸微调)
        margin_r = max(6, min(12, rw // 30))
        margin_b = max(5, min(10, rh // 30))
        cx = int(x0 * img.width) + rw - tw - margin_r
        cy = int(y0 * img.height) + rh - th - margin_b
        # 白字 + 1px 黑描边(夜视可读)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                draw.text((cx + dx, cy + dy), text, fill=(0, 0, 0), font=font)
        draw.text((cx, cy), text, fill=(255, 255, 255), font=font)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return jpeg


def overlay_osd(jpeg: bytes, lines: list[str], alpha: int = 160,
                position: str = "top-left") -> bytes:
    """在截图上合成 OSD 文字(时间戳/通道标签), **背景全透明**。

    lines 每行绘制; position='top-left'(默认, 时间戳) / 'bottom-right'(通道名),
    避免时间戳遮挡通道名。为可读性画 2px 黑色描边(非背景框), 画面不被遮挡。
    优先 PIL(自动尝试插件内置思源黑体, 与预览同字体); 失败静默返回原图。
    """
    try:
        from PIL import Image, ImageDraw, ImageFont

        img = Image.open(io.BytesIO(jpeg)).convert("RGB")
        font = None
        # 同预览 OSD 字体源: 插件内置思源黑体优先 → 系统字体兜底(避免截图/预览不一致)
        font_path = _find_cjk_font()
        if font_path:
            try:
                font = ImageFont.truetype(font_path, max(12, img.height // 40))
            except OSError:
                font = None
        if font is None:
            try:
                font = ImageFont.load_default(size=max(10, img.height // 44))
            except TypeError:  # Pillow <10 无 size 参数
                font = ImageFont.load_default()

        draw = ImageDraw.Draw(img)
        pad = 2
        # 行块尺寸
        row_sizes = []
        for line in lines:
            try:
                box = draw.textbbox((0, 0), line, font=font)
                row_sizes.append((box[2] - box[0], box[3] - box[1]))
            except AttributeError:  # 极老 Pillow
                row_sizes.append(draw.textsize(line, font=font))

        def _draw_text(x: int, y: int, text: str, fill=(255, 255, 255)) -> None:
            """白字 + 1px 黑描边(简洁低调, 夜视/亮底均清晰)。"""
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    draw.text((x + dx, y + dy), text, fill=(0, 0, 0), font=font)
            draw.text((x, y), text, fill=fill, font=font)

        if position == "bottom-right":
            # 白字黑描边, 从右下角向上排; 字号适中(不喧宾夺主)
            x0 = img.width - 4 - max(w for w, _h in row_sizes) - 2 * pad
            y = img.height - 4 - sum(h for _w, h in row_sizes) - 2 * pad * len(row_sizes)
            for (line, (w, h)) in zip(lines, row_sizes):
                x = img.width - 4 - w - 2 * pad
                _draw_text(x, y, line, fill=(255, 255, 255))
                y += h + 2 * pad + 4
        else:
            y = 6
            for line in lines:
                try:
                    box = draw.textbbox((0, 0), line, font=font)
                    tw, th = box[2] - box[0], box[3] - box[1]
                except AttributeError:
                    tw, th = draw.textsize(line, font=font)
                _draw_text(2 + pad, y + pad, line)
                y += th + 2 * pad + 4
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        return buf.getvalue()
    except Exception:  # noqa: BLE001 - 叠加失败返回原图
        return jpeg


async def async_h264_snapshot(h264: bytes, width: int = 640, codec: str = "h264") -> bytes | None:
    """H.264/H.265 Annex-B → JPEG(ffmpeg 子进程抽首帧)。"""
    return await async_video_snapshot(h264, width, codec)


async def async_video_snapshot(
    video: bytes, width: int = 640, codec: str = "h264"
) -> bytes | None:
    """H.264/H.265 Annex-B → JPEG(ffmpeg 抽首帧; codec='h264'|'h265')。"""
    import subprocess

    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        _LOGGER.warning(
            "ffmpeg not found; cloud snapshot unavailable "
            "(请安装 ffmpeg 并加入 PATH, HA 集成依赖已声明)"
        )
        return None
    fmt = "hevc" if codec == "h265" else "h264"

    def _run() -> bytes | None:
        proc = subprocess.Popen(  # noqa: S603
            [
                ffmpeg, "-hide_banner", "-loglevel", "error",
                "-f", fmt, "-i", "pipe:0",
                "-frames:v", "1",
                "-vf", "scale='min(%d,iw)':-2" % width,
                "-q:v", "4",
                "-f", "image2", "-c:v", "mjpeg", "pipe:1",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            out, _err = proc.communicate(video, timeout=25)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return None
        if proc.returncode != 0 or not out:
            return None
        return out

    return await asyncio.get_running_loop().run_in_executor(None, _run)


def _find_cjk_font() -> str | None:
    """定位中文字体(预览 OSD drawtext 用); 优先插件内置思源黑体。

    ★ 字体打包进插件(README 仅复制源码/自定义组件, 不依赖系统):
      custom_components/lechange_door_lock/fonts/SourceHanSansSC-Regular.otf
      (思源黑体 OFL 许可, 含常用汉字; 系统更换/无字库仍可显示中文)。
    ★ 系统 ffmpeg(Windows) 的 drawtext 在**未指定 fontfile** 时会因
      fontconfig 缺配置而段错误(实测 rc=3221225477), 因此必须总是返回一个
      存在的绝对路径字体(插件内置优先, 系统字体兜底)。
    """
    candidates = []
    # 插件内置字体(绝对稳定)
    pkg_dir = os.path.dirname(os.path.abspath(__file__))
    candidates += [
        os.path.join(pkg_dir, "fonts", "SourceHanSansSC-Regular.otf"),
    ]
    # 系统字体兜底(仅内置缺失时)
    candidates += (
        r"C:\Windows\Fonts\msyh.ttc",          # Windows 微软雅黑
        r"C:\Windows\Fonts\simhei.ttf",        # 黑体
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        # ASCII 兜底(避免无 fontfile 触发 ffmpeg fontconfig 崩溃)
        r"C:\Windows\Fonts\arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    )
    for cand in candidates:
        try:
            if os.path.exists(cand):
                return cand
        except OSError:
            continue
    return None


def _drawtext_filter(text: str, y_frac: float, fontsize: int,
                     color: str = "white", border: str = "black",
                     fontfile: str | None = None,
                     dynamic_time: bool = False,
                     pos: str = "top-left",
                     enable: str | None = None) -> str:
    """drawtext 滤镜串(白字黑描边; pos='top-left' 时间戳 / 'bottom-right' 通道名)。

    fontfile: 中文字体路径(选了可显示中文); None=默认字体(仅 ASCII 安全)。
    dynamic_time=True → text 用 '%{pts:hms}', 帧时间戳随播放走动。
    enable: 时间区间('between(t,0,1)') — 用于逐秒叠加真实时间戳段。

    ★ Windows 绝对路径巨坑: drawtext 的选项分隔符是 ':', 而盘符路径含 ':'。
      解决: 路径中所有 ':' 转义为 '\\:'(实测 ffmpeg 正确解析)。生产环境
      一律用绝对路径(相对路径随 CWD 漂移), 转义后即安全。
    """
    if dynamic_time:
        # 帧时间戳(播放时走动; 依赖系统 ffmpeg 完整版, 支持 localtime 可升级此处)
        text_esc = "%{pts\\:hms}"
    else:
        # 静态文字: 冒号是滤镜选项分隔符 → 转义为 ':'(raw 串内反斜杠冒号)
        text_esc = text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    font_opt = ""
    if fontfile:
        # 滤镜内 ':' 是选项分隔符 → 盘符冒号转义 'C\:/...';
        # ★ 路径同时正斜杠化(反斜杠在滤镜串中需双重转义, 不同 ffmpeg 版本敏感)
        font_opt = ":fontfile='" + fontfile.replace("\\", "/").replace(":", "\\:") + "'"
    if pos == "bottom-right":
        # 右下角: x=w-tw-margin, y=h-th-margin(自动适配文字宽高)
        xy = "x=w-text_w-12:y=h-text_h-10"
    else:
        xy = "x=8:y=h*%f" % y_frac
    enable_opt = ":enable='%s'" % enable if enable else ""
    return (
        "drawtext=text='%s':%s:fontsize=%d:fontcolor=%s:"
        "borderw=2:bordercolor=%s:box=0%s%s"
        % (text_esc, xy, fontsize, color, border, font_opt, enable_opt)
    )


async def async_osd_h264(
    h264: bytes, texts: list[str], width: int = 640, overlay: bool = True,
    codec: str = "h264", seconds: float = 10.0,
) -> bytes | None:
    """H.264/H.265 兼容入口(保留旧名)。"""
    return await async_osd_video(h264, texts, width, overlay, codec, seconds)


async def async_osd_video(
    video: bytes,
    texts: list[str],
    width: int = 640,
    overlay: bool = True,
    codec: str = "h264",
    seconds: float = 10.0,
) -> bytes | None:
    """H.264/H.265 → 烧录 OSD(动态时间戳+通道名) → 重编码输出(保持原编码)。

    overlay=False: 纯重编码(不烧字); 编码按 codec 选择 libx264/libx265(HEVC 优先)。
    OSD 布局: 时间戳左上(逐秒叠加真实时间, enable 区间), 通道名右下。
    seconds: 预估视频时长 → 每 1s 一段 drawtext, 文本=该秒真实时间
             (%Y-%m-%d %H:%M:%S), 播放时逐秒更新(不依赖 localtime 变量)。
    """
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        _LOGGER.warning("ffmpeg not found; preview OSD unavailable")
        return None
    fmt = "hevc" if codec == "h265" else "h264"
    enc = "libx265" if codec == "h265" else "libx264"

    def _run() -> bytes | None:
        vf_parts: list[str] = []
        if overlay:
            size = max(14, width // 24)
            fontfile = _find_cjk_font()
            start = datetime.now()
            # 逐秒时间戳段: 每 1s 一个 enable 区间, 文本=该秒真实时间
            for i in range(max(1, int(seconds))):
                seg_ts = (start + timedelta(seconds=i)).strftime("%Y-%m-%d %H:%M:%S")
                enable = "between(t,%d,%d)" % (i, i + 1)
                vf_parts.append(
                    _drawtext_filter(seg_ts, 0.02, size, fontfile=fontfile,
                                     pos="top-left", enable=enable)
                )
            # 通道名 → 右下角(last 区间一直显示)
            for t in texts:
                vf_parts.append(
                    _drawtext_filter(t, 0.0, size, fontfile=fontfile, pos="bottom-right")
                )
        vf = ",".join(vf_parts) if vf_parts else "null"
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-f", fmt, "-i", "pipe:0",
            "-vf", vf,
            "-c:v", enc, "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-f", fmt, "pipe:1",
        ]
        proc = subprocess.Popen(  # noqa: S603
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            out, _err = proc.communicate(video, timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return None
        if proc.returncode != 0 or not out:
            _LOGGER.debug("preview OSD re-encode failed: %s", _err[-200:] if _err else "")
            return None
        return out

    return await asyncio.get_running_loop().run_in_executor(None, _run)
