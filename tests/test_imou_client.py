"""Tests for the x-pcs signing client (fake aiohttp session, no network)."""

import asyncio
import base64
import hashlib
import hmac
import json
import ssl
import uuid
from datetime import datetime

import pytest

import lechange_door_lock.imou_client as ic
from lechange_door_lock.imou_client import ImouAPIError, ImouClient


# --------------------------------------------------------------------- keys
class TestKeyDerivation:
    def test_post_login_keys_from_token(self):
        token = "6i9lssbd46hkg31dkg9qr0301rae55kg"
        client = ImouClient(None, token=token)
        assert client._key1 == hashlib.md5(token.encode()).hexdigest().lower()
        assert client._key2 == hashlib.sha256(token.encode()).hexdigest().lower()

    def test_login_keys_are_double_hashed(self):
        assert ic._md5_hex_lower(ic._md5_hex_lower("pw")) != ic._md5_hex_lower("pw")
        assert ic._sha256_hex_lower(ic._sha256_hex_lower("pw")) != ic._sha256_hex_lower("pw")


# ---------------------------------------------------------------- signing
def _freeze_clock(monkeypatch):
    class FakeDT:
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003
            return datetime(2026, 9, 3, 0, 0, 0, tzinfo=tz)

    monkeypatch.setattr(ic, "datetime", FakeDT)
    monkeypatch.setattr(ic.random, "choices", lambda seq, k=64: list("a") * k)
    monkeypatch.setattr(ic, "_build_client_ua", lambda *a, **k: "FIXED_UA")


def _expected_signature(path, body, username, key, session_id, digest_line):
    parts = [
        "POST",
        path,
        digest_line,
        "application/json; charset=utf-8",
        "x-pcs-apiver:" + ic.APIVER,
        "x-pcs-client-ua:FIXED_UA",
        "x-pcs-date:2026-09-03T00:00:00Z",
        "x-pcs-nonce:" + "a" * 64,
    ]
    if session_id:
        parts.append("x-pcs-session-id:" + session_id)
    parts.append("x-pcs-username:" + username)
    canonical = "\n".join(parts) + "\n"
    return base64.b64encode(
        hmac.new(key.encode(), canonical.encode(), hashlib.sha256).digest()
    ).decode()


class TestSignPayload:
    def test_full_headers_with_session(self, monkeypatch):
        _freeze_clock(monkeypatch)
        path = "/pcs/v1/user.account.Login"
        body = b'{"data":{"timezoneOffset":480}}'
        headers = ic._sign_payload(
            "POST", path, body, "uuid\\internal_user", "k1", "k2", "sess123"
        )
        md5b = base64.b64encode(hashlib.md5(body).digest()).decode()
        shab = base64.b64encode(hashlib.sha256(body).digest()).decode()
        assert headers["x-pcs-date"] == "2026-09-03T00:00:00Z"
        assert headers["x-pcs-nonce"] == "a" * 64
        assert headers["x-pcs-apiver"] == "191204"
        assert headers["x-pcs-session-id"] == "sess123"
        assert headers["x-pcs-username"] == "uuid\\internal_user"
        assert headers["x-pcs-signature"] == _expected_signature(
            path, body, "uuid\\internal_user", "k1", "sess123", md5b
        )
        assert headers["x-pcs-signature-sha256"] == _expected_signature(
            path, body, "uuid\\internal_user", "k2", "sess123", shab
        )

    def test_no_session_header_and_different_signature(self, monkeypatch):
        _freeze_clock(monkeypatch)
        path = "/pcs/v1/user.account.GetToken"
        body = b'{"data":{}}'
        headers = ic._sign_payload("POST", path, body, "account\\13800000000", "k1", "k2")
        assert "x-pcs-session-id" not in headers
        with_sess = ic._sign_payload(
            "POST", path, body, "uuid\\u", "k1", "k2", "sess123"
        )
        assert headers["x-pcs-signature"] != with_sess["x-pcs-signature"]


# --------------------------------------------------------- fake transport
def _resp(payload: dict, status: int = 200):
    class FakeResponse:
        def __init__(self):
            self.status = status
            self.headers = {}

        async def text(self):
            return json.dumps(payload)

        async def read(self):
            return b""

    return FakeResponse()


class FakeSession:
    """Routes by API name; returns configurable responses (may be awaitable)."""

    def __init__(self, handlers: dict):
        self.handlers = handlers  # api_name -> callable() -> FakeResponse
        self.calls: list[str] = []       # api names
        self.calls_full: list[tuple[str, dict]] = []  # (api, kwargs)

    async def post(self, url, **kwargs):
        api = url.split("/pcs/v1/", 1)[1]
        self.calls.append(api)
        self.calls_full.append((api, kwargs))
        handler = self.handlers.get(api)
        if handler is None:
            raise AssertionError(f"unexpected API call: {api}")
        result = handler()
        if asyncio.iscoroutine(result):
            result = await result
        return result


class TestHttpPostAndErrors:
    def _client(self, session, token="abc"):
        return ImouClient(
            session,
            username="13800000000",
            password="pw",
            session_id="sess123",
            token=token,
            internal_username="internal_user",
            api_host="https://gw.example.com",
        )

    async def test_success(self):
        client = self._client(FakeSession({"device.list.BasicList": lambda: _resp({"code": 10000, "data": {"deviceList": []}})}))
        data = await client.async_post("device.list.BasicList", {})
        assert data == {"deviceList": []}

    async def test_business_error_raised(self):
        # 13924 = 业务参数/数据错误(非认证类,不触发重登)
        session = FakeSession({"device.list.BasicList": lambda: _resp({"code": 13924, "desc": ""})})
        client = self._client(session)
        with pytest.raises(ImouAPIError) as err:
            await client.async_post("device.list.BasicList", {})
        assert err.value.code == 13924

    async def test_http_error_raised(self):
        session = FakeSession({"device.list.BasicList": lambda: _resp({"code": 0}, status=500)})
        client = self._client(session)
        with pytest.raises(ImouAPIError) as err:
            await client.async_post("device.list.BasicList", {})
        assert err.value.code == 500

    async def test_network_error_mapped(self):
        async def boom():
            raise __import__("aiohttp").ClientError("boom")

        client = self._client(FakeSession({"device.list.BasicList": boom}))
        with pytest.raises(ImouAPIError) as err:
            await client.async_post("device.list.BasicList", {})
        assert err.value.code == -2

    async def test_tls_failure_mapped(self):
        async def bad_tls():
            raise ssl.SSLCertVerificationError("cert verify failed")

        client = self._client(FakeSession({"device.list.BasicList": bad_tls}))
        with pytest.raises(ImouAPIError) as err:
            await client.async_post("device.list.BasicList", {})
        assert err.value.code == -3

    async def test_auth_failure_triggers_relogin_once(self):
        token_calls = {"n": 0}
        basic_calls = {"n": 0}

        def get_token():
            return _resp(
                {
                    "code": 10000,
                    "data": {
                        "sessionId": "S-fresh",
                        "token": "T-fresh",
                        "username": "lc1n_internal",
                        "userId": 42,
                        "entryUrlV2": "https://gw.example.com:443",
                    },
                }
            )

        def basic():
            basic_calls["n"] += 1
            if basic_calls["n"] == 1:
                # 12002 = token 被作废(2026-09-03 实验:同账号新登录作废旧 token)
                return _resp({"code": 12002, "data": {}})
            return _resp({"code": 10000, "data": {"deviceList": [{"deviceId": "SN1"}]}})

        session = FakeSession(
            {
                "user.account.GetToken": get_token,
                "user.account.Login": lambda: _resp({"code": 10000, "data": {}}),
                "device.list.DeviceBasicInfoQueryV2": lambda: _resp({"code": 12002, "data": {}}),
                "device.list.BasicList": basic,
            }
        )
        client = self._client(session)
        devices = await client.async_get_devices()
        assert devices[0]["deviceId"] == "SN1"
        # 12002(token 作废)触发重登:QueryV2 一次 + BasicList 一次,各重登一回
        assert session.calls.count("user.account.GetToken") == 2
        assert basic_calls["n"] == 2
        assert client.session_id == "S-fresh"
        assert client.internal_username == "lc1n_internal"

    async def test_no_credentials_raises(self):
        client = ImouClient(FakeSession({}))
        with pytest.raises(ImouAPIError) as err:
            await client.async_ensure_session()
        assert err.value.code == 11010


class TestGrantingCredit:
    async def test_granting_credit_direct_code(self, monkeypatch):
        """终端授权链(线上协议校准):短信原码直传,无 CheckValidCode 中间步.

        报文形状:GrantingCredit {validCode:"049400", type:"phone",
        account:AES加密, isEncrypt:true} → 10000。
        加密函数打桩(不依赖 pycryptodome,CI 亦可确定);真实加密见 roundtrip 测试。
        """
        monkeypatch.setattr(ic, "_enc_account", lambda a: "ENC(" + a + ")")
        calls = []

        def granting():
            calls.append("grant")
            return _resp({"code": 10000, "data": {}})

        session = FakeSession({"user.account.GrantingCredit": granting})
        client = ImouClient(session, session_id="s", token="t", internal_username="u",
                            api_host="https://gw.example.com")
        await client.async_granting_credit("13800000000", "049400")
        assert calls == ["grant"]  # 无 CheckValidCode 中间步
        body = json.loads(session.calls_full[0][1]["data"])["data"]
        # validCode = 短信验证码原码;type 固定 "phone"
        assert body["validCode"] == "049400"
        assert body["type"] == "phone"
        assert body["isEncrypt"] is True
        assert body["account"] == "ENC(13800000000)"

    def test_enc_account_roundtrip(self):
        """真实 AES-GCM 加密可解回(线上客户端同款;无 pycryptodome 则跳过)."""
        pytest.importorskip("Crypto")
        from Crypto.Cipher import AES
        import base64 as _b64
        import hashlib as _hl

        enc = ic._enc_account("13800000000")
        assert enc and enc != "13800000000"
        key = _hl.sha256(b"F9TtRyv7X89nM0vp2EKOjdKLFnjlrN9rENCRYPTKEY").digest()
        raw = _b64.b64decode(enc)
        c = AES.new(key, AES.MODE_GCM, nonce=raw[:12])
        assert c.decrypt_and_verify(raw[12:-16], raw[-16:]).decode() == "13800000000"

    async def test_granting_credit_no_encryption_fallback(self):
        """无加密时仍按线上形状提交(明文 + isEncrypt:false 由方法内部兜底)."""
        session = FakeSession({
            "user.account.GrantingCredit": lambda: _resp({"code": 10000, "data": {}}),
        })
        client = ImouClient(session, session_id="s", token="t", internal_username="u",
                            api_host="https://gw.example.com")
        await client.async_granting_credit("13800000000", "049400", is_encrypt=False)
        body = json.loads(session.calls_full[0][1]["data"])["data"]
        assert body["validCode"] == "049400" and body["type"] == "phone"


class TestMessageDomain:
    """消息域:iot.message.* 用 apiver=V10.2.2 + charset=UTF-8."""

    def _client(self, session) -> ImouClient:
        return ImouClient(session, session_id="s", token="t", internal_username="u",
                          api_host="https://gw.example.com")

    async def test_secret_list_uses_msg_domain(self):
        session = FakeSession({
            "iot.message.SmartLockSecretListV2": lambda: _resp({"code": 10000, "data": {}}),
        })
        await self._client(session).async_smart_lock_secret_list("SN", "PID")
        _, kwargs = session.calls_full[0]
        headers = kwargs["headers"]
        assert headers["x-pcs-apiver"] == "V10.2.2"
        assert headers["Content-Type"] == "application/json; charset=UTF-8"

    async def test_secret_add_payload_and_domain(self):
        session = FakeSession({
            "iot.message.SmartLockSecretAdd": lambda: _resp({"code": 10000, "data": {}}),
        })
        await self._client(session).async_smart_lock_secret_add(
            "SN", "PID", "12345678", name="Test", number=-1, effect_days=1,
            usage_period="127-20260903T0000Z-20260904T2359Z",
        )
        api, kwargs = session.calls_full[0]
        assert kwargs["headers"]["x-pcs-apiver"] == "V10.2.2"
        body = json.loads(kwargs["data"])
        p = body["data"]
        assert p["tempKey"] == "12345678"
        assert p["type"] == 3
        assert p["number"] == -1
        assert p["createTime"] > 0
        assert p["expiredTime"] > p["createTime"]
        assert p["usagePeriod"].startswith("127-")
        assert p["keyId"] > 0  # 客户端生成

    async def test_secret_delete_domain_and_payload(self):
        session = FakeSession({
            "iot.message.SmartLockSecretDelete": lambda: _resp({"code": 10000, "data": {}}),
        })
        await self._client(session).async_smart_lock_secret_delete(
            "SN", "PID", 67330355, extra={"type": 3, "name": "x"}
        )
        api, kwargs = session.calls_full[0]
        assert kwargs["headers"]["x-pcs-apiver"] == "V10.2.2"
        body = json.loads(kwargs["data"])
        assert body["data"]["keyId"] == 67330355
        assert body["data"]["name"] == "x"


class TestSmsLogin:
    async def test_send_sms_code_payload(self):
        session = FakeSession({"common.validcode.GetValidCode": lambda: _resp({"code": 10000, "data": {}})})
        client = ImouClient(session, session_id="s", token="t", internal_username="u",
                            api_host="https://gw.example.com")
        await client.async_send_sms_code("13800000000")
        assert session.calls == ["common.validcode.GetValidCode"]

    async def test_login_sms_sets_session(self):
        session = FakeSession({
            "user.account.GetTokenBySMS": lambda: _resp({
                "code": 10000,
                "data": {"sessionId": "S-sms", "token": "T-sms", "username": "lc1n_sms",
                         "newUser": False, "entryUrlV2": "https://gw.example.com:443"},
            }),
            "user.account.Login": lambda: _resp({"code": 10000, "data": {}}),
        })
        client = ImouClient(session, api_host="https://gw.example.com")
        data = await client.async_login_sms("13800000000", "123456")
        assert data["session_id"] == "S-sms"
        assert client.session_id == "S-sms"
        assert client.internal_username == "lc1n_sms"
        assert client.token == "T-sms"
        # 短信登录无密码:自动重登不可用
        assert client.password == ""
        # 密钥派生与 GetToken 一致(md5/sha256(token))
        assert client._key1 == hashlib.md5(b"T-sms").hexdigest().lower()

    async def test_login_sms_no_session_raises(self):
        session = FakeSession({
            "user.account.GetTokenBySMS": lambda: _resp({"code": 2011, "desc": "invalid code"})
        })
        client = ImouClient(session, api_host="https://gw.example.com")
        with pytest.raises(ImouAPIError) as err:
            await client.async_login_sms("13800000000", "000000")
        assert err.value.code == 2011


class TestGetDevicesNormalization:
    async def test_full_normalization(self):
        device_payload = {
            "deviceList": [
                {
                    "deviceId": "SN001",
                    "productId": "SKG8J5R0",
                    "name": "R10-M0X-0000",
                    "deviceModel": "R10-M0X",
                    "catalog": "SmartLock",
                    "subCategory": "#SmartLock",
                    "status": "sleep",
                    "lockState": "beClosed",
                    "version": "1.000.0000000.0.R.251027",
                    "channelNum": 2,
                    "streamEntryAddrV3": "nginxdeviceproxy-online-hz.imou.com:443",
                    "channelList": [
                        {
                            "channelId": 0,
                            "channelName": "主摄像头",
                            "productId": "SKG8J5R0",
                            "status": "sleep",
                            "functions": ["unlock", "talk", "ptz", "snapshot"],
                            "mediaConfig": json.dumps(
                                {"streamUrl": "nginxdeviceproxy-online-hz.imou.com:443"}
                            ),
                        }
                    ],
                    "propertiesMap": "{\"106200\":[]}",
                }
            ]
        }
        session = FakeSession(
            {
                "device.list.DeviceBasicInfoQueryV2": lambda: _resp({"code": 10000, "data": device_payload}),
            }
        )
        client = ImouClient(
            session, session_id="s", token="t", internal_username="u",
            api_host="https://gw.example.com",
        )
        devices = await client.async_get_devices()
        dev = devices[0]
        assert dev["name"] == "R10-M0X-0000"
        assert dev["lockState"] == "beClosed"
        assert dev["channels"][0]["functions"] == ["unlock", "talk", "ptz", "snapshot"]
        assert dev["stream_entry"] == "nginxdeviceproxy-online-hz.imou.com:443"

    async def test_is_lock_detection(self):
        from lechange_door_lock.imou_client import ImouClient as IC

        assert IC.is_lock(
            {"catalog": "SmartLock", "channels": [{"functions": []}]}
        ) is True
        assert IC.is_lock(
            {"catalog": "Camera", "channels": [{"functions": ["unlock"]}]}
        ) is True
        assert IC.is_lock(
            {"catalog": "Camera", "channels": [{"functions": ["talk"]}]}
        ) is False


# ------------------------------------------------- ref encoding (线上协议)
def _model_raw() -> dict:
    """带服务/属性的匿名化模型(remoteOpenDoor ref=26600 等)."""
    return {
        "properties": [
            {"identifier": "openDoorCombined", "ref": "106400",
             "dataType": {"type": "bool", "specs": {}}},
            {"identifier": "sdl_indoorOpenMode", "ref": "171700",
             "dataType": {"type": "enum", "specs": {"list": [
                 {"value": "1", "desc": "普通开门模式"},
                 {"value": "2", "desc": "童锁模式"}]}}},
            {"identifier": "sdl_openDoorKey", "ref": "166300",
             "dataType": {"type": "struct", "specs": [
                 {"identifier": "key", "ref": "166301",
                  "dataType": {"type": "text", "specs": {}}},
                 {"identifier": "callID", "ref": "166302",
                  "dataType": {"type": "text", "specs": {}}}]},
             "accessMode": "w"},
        ],
        "services": [
            {"identifier": "remoteOpenDoor", "ref": "26600",
             "name": "远程开门", "inputData": [], "outputData": []},
            {"identifier": "CallAnswer", "ref": "27100",
             "name": "呼叫接听",
             "inputData": [{"identifier": "userInfo", "ref": "27101",
                            "dataType": {"type": "text", "specs": {}}}],
             "outputData": []},
            {"identifier": "SetVoiceReply", "ref": "27400",
             "name": "设置语音答复",
             "inputData": [{"identifier": "index", "ref": "27401",
                            "dataType": {"type": "int", "specs": {}}},
                           {"identifier": "relateType", "ref": "27402",
                            "dataType": {"type": "enum", "specs": {}}}],
             "outputData": []},
        ],
    }


class TestRefEncoding:
    """iot.control 写路径的 ref 编码(2026-09-03 线上实测协议)."""

    def _model(self):
        return ic.ModelInfo(_model_raw())

    def test_service_ref(self):
        model = self._model()
        assert model.service_ref("remoteOpenDoor") == "26600"
        # 未知服务回退原值
        assert model.service_ref("customSvc") == "customSvc"

    def test_encode_service_input(self):
        model = self._model()
        assert model.encode_service_input("remoteOpenDoor", {}) == {}
        assert model.encode_service_input("CallAnswer", {"userInfo": "u"}) == {
            "27101": "u"
        }
        # int/enum 原样
        assert model.encode_service_input(
            "SetVoiceReply", {"index": 1, "relateType": 2}) == {"27401": 1, "27402": 2}

    def test_encode_properties(self):
        model = self._model()
        assert model.encode_properties({"openDoorCombined": True}) == {"106400": 1}
        assert model.encode_properties({"openDoorCombined": False}) == {"106400": 0}
        # enum 原样
        assert model.encode_properties({"sdl_indoorOpenMode": "2"}) == {"171700": "2"}
        # struct 递归 ref 键
        assert model.encode_properties(
            {"sdl_openDoorKey": {"key": "k1", "callID": "c1"}}
        ) == {"166300": {"166301": "k1", "166302": "c1"}}
        # 未知键原样回退
        assert model.encode_properties({"unknown": 3}) == {"unknown": 3}

    async def test_set_service_payload_uses_ref(self):
        """async_set_service 发送的 body: service=ref、键=ref、authInfo->client."""
        session = FakeSession(
            {
                "iot.manager.QueryModelInfo": lambda: _resp(
                    {"code": 10000, "data": {"modelJson": _model_raw()}}
                ),
                "iot.control.SetService": lambda: _resp(
                    {"code": 10000, "data": {"outputData": {}, "channelId": 0}}
                ),
            }
        )
        client = ImouClient(
            session, session_id="s", token="t", internal_username="u",
            api_host="https://gw.example.com",
        )
        await client.async_set_service(
            "SN1", "SKG8J5R0", "CallAnswer", {"userInfo": "u1"},
            channel_id="0", auth_info="A1",
        )
        api, kwargs = session.calls_full[-1]
        assert api == "iot.control.SetService"
        body = json.loads(kwargs["data"].decode())
        assert body["data"]["service"] == "27100"
        assert body["data"]["inputData"] == {"27101": "u1"}
        assert body["data"]["client"] == {"authId": "A1"}
        assert "serviceName" not in body["data"]
        assert "authInfo" not in body["data"]

    async def test_set_properties_payload_uses_ref(self):
        session = FakeSession(
            {
                "iot.manager.QueryModelInfo": lambda: _resp(
                    {"code": 10000, "data": {"modelJson": _model_raw()}}
                ),
                "iot.control.SetProperties": lambda: _resp({"code": 10000, "data": {}}),
            }
        )
        client = ImouClient(
            session, session_id="s", token="t", internal_username="u",
            api_host="https://gw.example.com",
        )
        await client.async_set_properties(
            "SN1", "SKG8J5R0", {"openDoorCombined": True}, channel_id="0"
        )
        api, kwargs = session.calls_full[-1]
        assert api == "iot.control.SetProperties"
        body = json.loads(kwargs["data"].decode())
        assert body["data"]["properties"] == {"106400": 1}

    async def test_switch_payload_roundtrip_bool(self):
        """bool 属性经 encode->decode 往返保持布尔语义."""
        model = self._model()
        encoded = model.encode_properties({"openDoorCombined": True})
        decoded = model.decode_properties(encoded)
        assert decoded["openDoorCombined"] is True


# ----------------------------------------------------------- client-ua
class TestClientUA:
    """client-ua 终端特征:线上 Android 客户端对齐."""

    @staticmethod
    def _decode(ua: str) -> dict:
        return json.loads(base64.b64decode(ua.encode()).decode())

    def test_android_device_signature(self):
        """UA 字段(线上样本): clientType=phone / clientOS=Android 大写 /
        clientVersion=V10.2.2 / clientOV="Android 14" / language=zh_CN /
        timezoneOffset=28800(秒) / 无 country / 无 darkMode."""
        ua = self._decode(ic._build_client_ua("11111111-2222-3333-4444-555555555555"))
        pool = {m for m, _b, _o in ic._DEVICE_POOL}
        brand_of = {m: b for m, b, _o in ic._DEVICE_POOL}
        assert ua["clientType"] == "phone"
        assert ua["clientOS"] == "Android"
        assert ua["clientVersion"] == "V10.2.2"
        assert ua["clientOV"] == "Android 14"
        assert ua["language"] == "zh_CN"
        assert ua["timezoneOffset"] == "28800"
        assert "country" not in ua
        assert "darkMode" not in ua
        assert ua["terminalModel"] in pool
        assert ua["terminalBrand"] == brand_of[ua["terminalModel"]]
        assert ua["appid"] == ic.APP_ID
        assert ua["project"] == ic.PROJECT
        assert ua["clientProtocolVersion"] == ic.PROTO_VER

    def test_ua_stable_per_terminal(self):
        """同一 terminal_id → UA 恒定(每请求漂移本身即是特征)."""
        tid = "A1B2C3D4-0000-1111-2222-333344445555"
        u1 = ic._build_client_ua(tid)
        u2 = ic._build_client_ua(tid)
        assert u1 == u2

    def test_ua_diversifies_across_terminals(self):
        """不同 terminal_id → 分散到多机型(避免单一特征被聚类标记)."""
        models = {
            self._decode(ic._build_client_ua(str(uuid.UUID(int=i))) )["terminalModel"]
            for i in range(200)
        }
        assert len(models) >= 5  # 池 ≥18 机型, 200 个终端必命中多款

    def test_terminal_id_derived_to_16hex(self):
        """UA 内 terminalId = 线上 android_id 格式(16hex 小写)。

        持久化层 terminal_id 保持 UUID(兼容旧装), UA 内确定性派生 16hex,
        同一 terminal_id → 同一 16hex(平滑迁移), 16hex 输入原样透传。
        """
        tid = "A1B2C3D4-0000-1111-2222-333344445555"
        ua = self._decode(ic._build_client_ua(tid))
        assert ua["terminalId"] == ic._real_android_id(tid)
        assert len(ua["terminalId"]) == 16
        assert all(c in "0123456789abcdef" for c in ua["terminalId"])
        # 同输入恒定
        assert ic._real_android_id(tid) == ic._real_android_id(tid)
        # 16hex 原样透传
        assert ic._real_android_id("0123456789abcdef") == "0123456789abcdef"

    def test_ua_carries_ttid(self):
        """UA 带 ttid(线上样本: 32hex 无连字符)."""
        tid = "A1B2C3D4-0000-1111-2222-333344445555"
        ua = self._decode(ic._build_client_ua(tid))
        ttid = ua.get("ttid", "")
        assert len(ttid) == 32
        assert all(c in "0123456789abcdef" for c in ttid)
        assert ttid == ic._real_ttid(tid)  # 稳定

    def test_generated_terminal_id_is_uuid(self):
        """未提供 terminalId 时持久化层自动生成标准 UUID(UA 内派生为 16hex)."""
        ua1 = self._decode(ic._build_client_ua())
        ua2 = self._decode(ic._build_client_ua())
        # 每次调用会新生成 UUID → 派生 16hex 不同
        assert ua1["terminalId"] != ua2["terminalId"]
        for tid in (ua1["terminalId"], ua2["terminalId"]):
            assert len(tid) == 16
            assert all(c in "0123456789abcdef" for c in tid)

    def test_client_terminal_id_is_uuid(self):
        """ImouClient 无显式 terminalId 时生成 UUID 格式(旧 lechange-hass-* 弃用)."""
        client = ImouClient(None)
        assert len(client.terminal_id) == 36
        assert not client.terminal_id.startswith("lechange-hass-")

    def test_ua_participates_in_signature(self, monkeypatch):
        """UA 变化 → 签名变化(签名串包含 x-pcs-client-ua)."""
        _freeze_clock(monkeypatch)
        body = b'{"data":{}}'
        h1 = ic._sign_payload("POST", "/pcs/v1/x", body, "u", "k1", "k2")
        monkeypatch.setattr(
            ic, "_build_client_ua", lambda *a, **k: "OTHER_UA"
        )
        h2 = ic._sign_payload("POST", "/pcs/v1/x", body, "u", "k1", "k2")
        assert h1["x-pcs-client-ua"] == "FIXED_UA"
        assert h2["x-pcs-client-ua"] == "OTHER_UA"
        assert h1["x-pcs-signature"] != h2["x-pcs-signature"]
