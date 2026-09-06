"""DHAV 告警抓拍图解码.

容器结构(实测):
  [0x00-0x43]   DHAV 头; [0x33-0x42] IV(16×同一随机字节, 每图不同)
  [0x44-0x143]  加密区 256B = JPEG 头(SOI+APP0+FFEF+DQT×2+FFFE 伪段)
  [0x144-0x1EA] 零填充
  [0x1EB..]     'dhav' 子包 → SOF0 起全程明文 JPEG
  [尾部]        ffd9 + 'dhav' + 大小页脚

KDF(与实时流同构, 两套密码体系):
  password = MD5("admin:Login to {device_id}:{安全码}").hex().upper()
  key      = PBKDF2-HMAC-SHA256(password, salt=device_id, 20000轮, 32B)
加密 AES-256-OFB, IV=data[0x33:0x43]。

历史告警图始终用「安全码」(设备密码的出厂代)解密。
"""

from __future__ import annotations

import hashlib
import logging
import struct

_LOGGER = logging.getLogger(__name__)

try:
    from Crypto.Cipher import AES
except ImportError:  # pragma: no cover - pycryptodome 缺失时仅告警图不可用
    AES = None

IV_OFF = 0x33
ENC_START = 0x44
ENC_LEN = 256


def derive_key(device_id: str, security_code: str) -> bytes:
    """安全码 → AES-256 密钥(告警图/出厂代 KDF)。"""
    fmt = "admin:Login to %s:%s" % (device_id, security_code)
    pwd = hashlib.md5(fmt.encode()).hexdigest().upper().encode()
    return hashlib.pbkdf2_hmac("sha256", pwd, device_id.encode(), 20000, 32)


def _head_upto_second_dqt(head: bytes) -> bytes:
    """头段含 FFFE(254B, 越界伪段) — 裁到第二个 DQT 结束。"""
    p = 2
    dqt_seen = 0
    while p < len(head) - 3:
        m = head[p + 1]
        if m == 0xD8:
            p += 2
            continue
        ln = struct.unpack(">H", head[p + 2:p + 4])[0]
        p += 2 + ln
        if m == 0xDB:
            dqt_seen += 1
            if dqt_seen == 2:
                return head[:p]
    return head[:64]  # 异常兜底


def dav_to_jpeg(data: bytes, key: bytes | None = None) -> bytes:
    """DHAV 告警抓拍 → 原厂 JPEG bytes。失败抛 ValueError。"""
    if AES is None:
        raise ValueError("pycryptodome not installed")
    if len(data) < ENC_START + ENC_LEN or data[:4] != b"DHAV":
        raise ValueError("not DHAV container")
    iv = data[IV_OFF:IV_OFF + 16]
    head = AES.new(key, AES.MODE_OFB, iv=iv).decrypt(data[ENC_START:ENC_START + ENC_LEN])
    if head[:2] != b"\xff\xd8":
        raise ValueError("decrypt failed (wrong key?)")
    head = _head_upto_second_dqt(head)
    so = -1
    for marker in (b"\xff\xc0", b"\xff\xc2"):
        # ★ 起点 ENC_START+ENC_LEN(明文区): 原从密文区开始扫, 0.4% 概率
        #   密文伪命中 \xff\xc0/\xff\xc2 拼出坏图(静默)。明文实际从 0x1EB 起,
        #   跳过整个密文块后搜索更稳。
        so = data.find(marker, ENC_START + ENC_LEN)
        if so >= 0:
            break
    if so < 0:
        raise ValueError("SOF0 not found (no plaintext body)")
    end = data.rfind(b"\xff\xd9")
    if end < 0 or end < so:
        raise ValueError("EOI not found")
    return head + data[so:end + 2]


def media_bytes_to_jpeg(data: bytes, device_id: str, security_code: str) -> bytes:
    """告警 picUrl 下载内容 → JPEG。

    云端两种形态(实测): 明文 JPEG(ffd8 开头, 直接返回) 与
    DHAV 加密容器(用安全码解密头)。其他格式抛 ValueError。
    """
    if data[:2] == b"\xff\xd8":
        return data
    if data[:4] == b"DHAV":
        return dav_to_jpeg(data, derive_key(device_id, security_code))
    raise ValueError("unknown media format: %s" % data[:8].hex())
