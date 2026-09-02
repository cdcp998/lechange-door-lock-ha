"""Tests for the x-pcs signing client (fake aiohttp session, no network)."""

import asyncio
import base64
import hashlib
import hmac
import json
import ssl
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
    monkeypatch.setattr(ic, "_build_client_ua", lambda: "FIXED_UA")


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
        self.calls: list[str] = []

    async def post(self, url, **kwargs):
        api = url.split("/pcs/v1/", 1)[1]
        self.calls.append(api)
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
        session = FakeSession({"device.list.BasicList": lambda: _resp({"code": 12002, "desc": ""})})
        client = self._client(session)
        with pytest.raises(ImouAPIError) as err:
            await client.async_post("device.list.BasicList", {})
        assert err.value.code == 12002

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
                return _resp({"code": 11010, "data": {}})  # session expired
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
        assert session.calls.count("user.account.GetToken") == 1
        assert basic_calls["n"] == 2
        assert client.session_id == "S-fresh"
        assert client.internal_username == "lc1n_internal"

    async def test_no_credentials_raises(self):
        client = ImouClient(FakeSession({}))
        with pytest.raises(ImouAPIError) as err:
            await client.async_ensure_session()
        assert err.value.code == 11010


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
