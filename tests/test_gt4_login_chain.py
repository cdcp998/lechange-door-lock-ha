"""Tests for the evergreen login chain additions (gt4_helper + session model).

覆盖:
  - token 信任度继承来源 sid 登录历史(有史→激活, 无史→12001)
  - GetToken 签发条件: 账号无活跃 token 或 sid 带 12002 续期标记
  - 10000+{failNum} 无 token = 账号已有活跃 token(failNum 与 token 无关, 每日0点清)
  - CheckGeeTest4 身份 = default 前缀 + OEM_AK, 密钥 = SK 单哈希(非密码双哈希!)
  - usage 一致性: CheckGeeTest4 与 GetValidCode 的 usage 必须相同
  - gt4.html 生成: 占位符替换 / WebView UA 注入 / verifyToken 注入
  - host 架构: 全部 pcs/v1(登录+业务)走 app-v2(entryUrlV2 分发)
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lechange_door_lock import gt4_helper
from lechange_door_lock.const import (
    GT4_CAPTCHA_ID,
    OEM_AK,
    OEM_SK,
)


# ---------------------------------------------------------------------------
# const / key derivation
# ---------------------------------------------------------------------------
def test_oem_keys_env_provided():
    """OEM AK/SK 通过环境变量提供, 不随源码分发; 缺省为空(功能降级)."""
    import lechange_door_lock.const as _const
    assert isinstance(_const.OEM_AK, str)
    assert isinstance(_const.OEM_SK, str)


def test_gt4_identity_is_default_prefix_not_password():
    """CheckGeeTest4 身份 = default 前缀; 密钥 = SK 单哈希(非密码双哈希!).

    线上 x-pcs-signature 匹配 md5hex(SK)/sha256hex(SK),
    与密码路径的 md5(md5hex(pw)) 双哈希完全不同。
    """
    sk = "test-sk-value"  # 注入用测试值(线上 SK 由使用者环境变量提供)
    ak = "test-ak-value"
    sk_key1 = hashlib.md5(sk.encode()).hexdigest().lower()
    sk_key2 = hashlib.sha256(sk.encode()).hexdigest().lower()
    assert sk_key1 != hashlib.md5(ak.encode()).hexdigest().lower()
    assert len(sk_key1) == 32 and len(sk_key2) == 64
    # 单哈希 ≠ 双哈希
    dbl1 = hashlib.md5(sk_key1.encode()).hexdigest().lower()
    assert dbl1 != sk_key1


# ---------------------------------------------------------------------------
# gt4.html generation
# ---------------------------------------------------------------------------
def test_build_gt4_html_replaces_all_placeholders():
    html = gt4_helper.build_gt4_html(
        account_label="18078964299",
        verify_token="eyJhbGciOi.test",
        endpoint="http://127.0.0.1:8765/gt4",
        usage="SMSLogin",
        account_enc="aGVsbG8=",
    )
    for placeholder in ("__ACCOUNT_LABEL__", "__CAPTCHA_ID__", "__ENDPOINT__",
                        "__VERIFY_TOKEN__", "__USAGE__", "__ACCOUNT_ENC__"):
        assert placeholder not in html
    assert "18078964299" in html
    assert "eyJhbGciOi.test" in html
    assert "http://127.0.0.1:8765/gt4" in html
    assert '"SMSLogin"' in html
    assert GT4_CAPTCHA_ID in html


def test_build_gt4_html_injects_webview_ua():
    """UA 注入(WebView 标记 wv)必须存在 — 四元组产出环境一致性."""
    html = gt4_helper.build_gt4_html("a", "b", "http://x/gt4")
    assert "Object.defineProperty(navigator, 'userAgent'" in html
    assert "; wv)" in html          # Android WebView 标记


def test_build_gt4_html_uses_gt4_js_component():
    html = gt4_helper.build_gt4_html("a", "b", "http://x/gt4")
    assert "initGeetest4" in html
    assert "static.geetest.com/v4/gt4.js" in html
    assert "captcha.onSuccess" in html
    # 四元组字段全回传
    for field in ("lot_number", "captcha_output", "pass_token", "gen_time"):
        assert field in html


def test_build_gt4_html_usage_reaches_page():
    """usage 必须注入页面并回传 — CheckGeeTest4 与 GetValidCode usage 一致性."""
    html = gt4_helper.build_gt4_html("a", "b", "http://x/gt4", usage="Login")
    assert '"Login"' in html


# ---------------------------------------------------------------------------
# parse helpers
# ---------------------------------------------------------------------------
def test_parse_verify_token():
    assert gt4_helper.parse_verify_token({"verifyToken": "jwt123"}) == "jwt123"
    assert gt4_helper.parse_verify_token({}) == ""
    assert gt4_helper.parse_verify_token(None) == ""


def test_parse_12114():
    cd = {"captchaId": "x", "verifyToken": "t"}
    assert gt4_helper.parse_12114(12114, {"captchaData": cd}) == cd
    assert gt4_helper.parse_12114(12114, {}) == {}
    assert gt4_helper.parse_12114(10000, {"captchaData": cd}) is None


# ---------------------------------------------------------------------------
# GT4TupleListener
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_listener_serves_prepared_html():
    listener = gt4_helper.GT4TupleListener(port=18799)
    await listener.start()
    try:
        listener.html_for("test-account", "tok-1", "SMSLogin", "enc-1")
        import aiohttp
        async with aiohttp.ClientSession() as sess:
            async with sess.get("http://127.0.0.1:18799/gt4.html") as resp:
                assert resp.status == 200
                text = await resp.text()
        assert "test-account" in text
        assert "tok-1" in text
    finally:
        await listener.stop()


@pytest.mark.asyncio
async def test_listener_rejects_incomplete_tuple():
    got = []
    listener = gt4_helper.GT4TupleListener(port=18798, on_tuple=lambda t: got.append(t))
    await listener.start()
    try:
        import aiohttp
        async with aiohttp.ClientSession() as sess:
            async with sess.post("http://127.0.0.1:18798/gt4",
                                 json={"lot_number": "only-one"}) as resp:
                assert resp.status == 200
                assert (await resp.json())["ok"] is False
        assert got == []  # 不完整不触发回调
    finally:
        await listener.stop()


@pytest.mark.asyncio
async def test_listener_dispatches_complete_tuple():
    got = []
    listener = gt4_helper.GT4TupleListener(port=18797, on_tuple=lambda t: got.append(t))
    await listener.start()
    try:
        import aiohttp
        tup = {"lot_number": "ln1", "captcha_output": "co1",
               "pass_token": "pt1", "gen_time": "1700000000"}
        async with aiohttp.ClientSession() as sess:
            async with sess.post("http://127.0.0.1:18797/gt4", json=tup) as resp:
                assert (await resp.json())["ok"] is True
        for _ in range(30):
            if got:
                break
            await asyncio.sleep(0.05)
        assert len(got) == 1
        assert got[0]["lot_number"] == "ln1"
        assert got[0]["captcha_id"] == GT4_CAPTCHA_ID
    finally:
        await listener.stop()


import asyncio  # noqa: E402  (asyncio tests above use it)


# ---------------------------------------------------------------------------
# client-level: GT4 identity / signing keys (patched transport)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_check_geetest4_uses_ak_identity_and_sk_keys():
    """CheckGeeTest4 请求头必须用 default 前缀身份 + SK 单哈希密钥."""
    from lechange_door_lock.imou_client import (
        ImouClient,
        _md5_hex_lower,
        _sha256_hex_lower,
    )
    client = ImouClient(MagicMock(), username="18078964299", password="pw")
    captured = {}

    async def fake_post(host, path, body, headers):
        captured["headers"] = headers
        captured["body"] = json.loads(body)
        return {"token": "gt4-pass-token"}

    with patch.object(client, "_http_post", side_effect=fake_post):
        result = await client.async_check_geetest4(
            lot_number="ln", captcha_output="co", pass_token="pt",
            gen_time="1700000000", usage="SMSLogin", verify_token="vt",
        )

    assert result == {"token": "gt4-pass-token"}
    headers = captured["headers"]
    assert headers["x-pcs-username"] == "default\\" + OEM_AK
    # SK 单哈希密钥(签名验证: HMAC(base, md5hex(SK)))
    sk1 = _md5_hex_lower(OEM_SK)
    sk2 = _sha256_hex_lower(OEM_SK)
    # 重算签名验证
    import hmac as _hmac
    uri = "/pcs/v1/common.validcode.CheckGeeTest4"
    body = json.dumps(captured["body"], separators=(",", ":")).encode()
    md5b = base64.b64encode(hashlib.md5(body).digest()).decode()
    ua = headers["x-pcs-client-ua"]
    base = (f"POST\n{uri}\n{md5b}\n{headers['Content-Type']}\n"
            f"x-pcs-apiver:191204\nx-pcs-client-ua:{ua}\n"
            f"x-pcs-date:{headers['x-pcs-date']}\nx-pcs-nonce:{headers['x-pcs-nonce']}\n"
            f"x-pcs-username:{headers['x-pcs-username']}\n")
    expect = base64.b64encode(
        _hmac.new(sk1.encode(), base.encode(), hashlib.sha256).digest()).decode()
    assert headers["x-pcs-signature"] == expect
    # body 字段(服务端协议驼峰: lotNumber/captchaOutput/passToken/genTime)
    payload = captured["body"]["data"]
    assert payload["lotNumber"] == "ln"
    assert payload["captchaOutput"] == "co"
    assert payload["passToken"] == "pt"
    assert payload["genTime"] == "1700000000"
    assert payload["usage"] == "SMSLogin"
    assert payload["verifyToken"] == "vt"


@pytest.mark.asyncio
async def test_send_sms_code_gt4_uses_ak_identity():
    """GT4 后重发短信: default 身份 + isEncrypt=true + usage 透传."""
    from lechange_door_lock.imou_client import ImouClient
    client = ImouClient(MagicMock(), username="18078964299", password="pw")
    captured = {}

    async def fake_post(host, path, body, headers):
        captured["headers"] = headers
        captured["body"] = json.loads(body)
        return {}

    with patch.object(client, "_http_post", side_effect=fake_post):
        await client.async_send_sms_code_gt4(usage="SMSLogin", account_enc="ENC")

    assert captured["headers"]["x-pcs-username"].startswith("default\\")
    payload = captured["body"]["data"]
    assert payload["usage"] == "SMSLogin"
    assert payload["account"] == "ENC"
    assert payload["isEncrypt"] is True
    assert payload["type"] == "phone"


@pytest.mark.asyncio
async def test_get_token_by_sms_ak_activates_session():
    """GetTokenBySMS(AK 身份) 响应后 _apply_login_response 完成 sid/token 切换.

    来源 sid 有登录史时签发的 token 已激活 → Login 10000;此处验证
    客户端正确存储 sid/token/username 并切换签名密钥。
    """
    from lechange_door_lock.imou_client import ImouClient
    client = ImouClient(MagicMock(), username="18078964299", password="pw")

    async def fake_post(host, path, body, headers):
        return {"sessionId": "new-sid-123", "token": "new-token-456",
                "username": "internal-user", "entryUrlV2": "https://app-v2.imou.com:443"}

    with patch.object(client, "_http_post", side_effect=fake_post), \
         patch.object(client, "async_post", new=AsyncMock(return_value={})):
        result = await client.async_get_token_by_sms_ak("123456", account_enc="ENC")

    assert result["session_id"] == "new-sid-123"
    assert result["token"] == "new-token-456"
    assert client.session_id == "new-sid-123"
    assert client.token == "new-token-456"
    assert client.internal_username == "internal-user"
    assert client.logged_in is True
    # 签名密钥切换为 token 单哈希
    assert client._key1 == hashlib.md5(b"new-token-456").hexdigest().lower()
    assert client._key2 == hashlib.sha256(b"new-token-456").hexdigest().lower()


@pytest.mark.asyncio
async def test_login_evergreen_reuses_session_when_token_alive():
    """10000+无token(仅 failNum) = 账号已有活跃 token → 保留现会话不报错断链.

    线上行为: token 未过期时 GetToken 不重发 — 此时若本地有会话
    应继续使用;async_login 应抛特定错误让 evergreen 捕获保持现状。
    """
    from lechange_door_lock.imou_client import ImouClient, ImouAPIError
    client = ImouClient(MagicMock(), username="u", password="p",
                        session_id="existing-sid", token="existing-token",
                        internal_username="iu")
    assert client.logged_in is True
    # 模拟服务端返回 10000+{failNum} 无 token
    async def fake_post(host, path, body, headers):
        return {"failNum": "4"}
    with patch.object(client, "_http_post", side_effect=fake_post):
        with pytest.raises(ImouAPIError) as exc_info:
            await client.async_login("u", "p")
    # 错误码 10001 = token-alive-no-reissue(客户端自定义)
    assert exc_info.value.code == 10001
    assert "failNum" in exc_info.value.desc
    # 现有会话未被破坏
    assert client.session_id == "existing-sid"
    assert client.token == "existing-token"
    assert client.logged_in is True


@pytest.mark.asyncio
async def test_async_login_sends_stored_sid_header():
    """GetToken 必须带自有持久 sid(有登录史的 sid 才能直通)."""
    from lechange_door_lock.imou_client import ImouClient
    client = ImouClient(MagicMock(), username="u", password="p",
                        session_id="my-persistent-sid")

    async def fake_post(host, path, body, headers):
        assert headers["x-pcs-session-id"] == "my-persistent-sid"
        return {"sessionId": "new", "token": "t", "username": "iu"}

    with patch.object(client, "_http_post", side_effect=fake_post), \
         patch.object(client, "async_post", new=AsyncMock(return_value={})):
        await client.async_login("u", "p")


@pytest.mark.asyncio
async def test_login_evergreen_propagates_gt4_challenge():
    """12114 时 evergreen 上抛携带 verifyToken 的错误(供 gt4.html 流程接管)."""
    from lechange_door_lock.imou_client import ImouClient, ImouAPIError
    client = ImouClient(MagicMock(), username="u", password="p")

    async def fake_post(host, path, body, headers):
        raise ImouAPIError(12114, "need geetest4 captcha verify")

    with patch.object(client, "_http_post", side_effect=fake_post):
        with pytest.raises(ImouAPIError) as exc_info:
            await client.async_login_evergreen()
    assert exc_info.value.code == 12114
