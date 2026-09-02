"""Tests for camera stream URL helpers (no Home Assistant runtime)."""

from lechange_door_lock.streams import build_rtsp_url, is_lan_host, split_host_port


class TestBuildRtspUrl:
    def test_lan_full_credentials(self):
        url = build_rtsp_url("192.168.1.10", 554, "admin", "secret", 0, 0)
        assert url == (
            "rtsp://admin:secret@192.168.1.10:554/cam/realmonitor?channel=1&subtype=0"
        )

    def test_channel_is_one_based(self):
        url = build_rtsp_url("192.168.1.10", 554, "user", "pw", 1, 1)
        assert "channel=2" in url and "subtype=1" in url

    def test_no_credentials(self):
        url = build_rtsp_url("192.168.1.10", 554, "", "", 0, 0)
        assert url.startswith("rtsp://192.168.1.10:554/")
        assert "@" not in url

    def test_special_chars_are_quoted(self):
        url = build_rtsp_url("192.168.1.10", 554, "a@b", "p:ss", 0, 0)
        assert "a%40b:p%3Ass@" in url

    def test_empty_host_gives_empty_url(self):
        assert build_rtsp_url("", 554, "admin", "pw", 0, 0) == ""

    def test_string_channel_id(self):
        url = build_rtsp_url("10.0.0.2", 554, "u", "p", "0", "1")
        assert "channel=1&subtype=1" in url


class TestSplitHostPort:
    def test_host_with_port(self):
        assert split_host_port("nginxproxy.example.com:443", 554) == (
            "nginxproxy.example.com",
            443,
        )

    def test_host_plain(self):
        assert split_host_port("nginxproxy.example.com", 554) == (
            "nginxproxy.example.com",
            554,
        )

    def test_host_with_scheme(self):
        assert split_host_port("https://gw.example.com:123", 554) == ("gw.example.com", 123)

    def test_empty(self):
        assert split_host_port("", 554) == ("", 554)


class TestIsLanHost:
    def test_ipv4_true(self):
        assert is_lan_host("192.168.1.10") is True

    def test_ipv4_with_port_true(self):
        assert is_lan_host("192.168.1.10:554") is True

    def test_domain_false(self):
        assert is_lan_host("nginxdeviceproxy-online-hz.imou.com:443") is False
        assert is_lan_host("app-v2.imou.com") is False

    def test_empty_false(self):
        assert is_lan_host("") is False
