from fastapi.testclient import TestClient

from mih.api.app import create_app
from mih.api.guard import client_address
from mih.config import ConfigError, Settings, parse_networks

TOKEN = "t" * 32


def _client(tmp_path, peer="203.0.113.7", **kw):
    app = create_app(Settings(data_dir=tmp_path, api_token=TOKEN, **kw))
    return TestClient(app, client=(peer, 50000))


def test_wrong_tokens_lock_the_address_out(tmp_path):
    with _client(tmp_path, auth_fails=3) as c:
        for _ in range(3):
            bad = c.get("/api/v1/channels", headers={"Authorization": "Bearer x"})
            assert bad.status_code == 401
        # Now even the right token waits: the address is locked for 15 minutes.
        locked = c.get("/api/v1/channels", headers={"Authorization": f"Bearer {TOKEN}"})
        assert locked.status_code == 429
        assert int(locked.headers["retry-after"]) > 800
        assert locked.json()["error"]["code"] == "too_many_requests"


def test_rate_limit_per_address_but_not_for_loopback(tmp_path):
    with _client(tmp_path, rate_limit=5) as c:
        codes = [c.get("/api/v1/health/live").status_code for _ in range(7)]
        assert codes[:5] == [200] * 5 and codes[5:] == [429, 429]
    with _client(tmp_path, peer="127.0.0.1", rate_limit=5) as c:
        assert {c.get("/api/v1/health/live").status_code for _ in range(10)} == {200}


def test_forwarded_address_counts_only_behind_a_trusted_proxy(tmp_path):
    with _client(tmp_path, peer="172.18.0.1", rate_limit=2) as c:

        def code(ip):
            return c.get("/api/v1/health/live", headers={"X-Forwarded-For": ip}).status_code

        for ip in ("198.51.100.1", "198.51.100.2"):
            assert [code(ip), code(ip)] == [200, 200]
        # Each forwarded client has its own count.
        assert code("198.51.100.1") == 429


def test_client_address_ignores_spoofed_hops():
    trusted = parse_networks("private")
    hops = [(b"x-forwarded-for", b"10.0.0.5, 198.51.100.9")]
    scope = {"client": ("172.18.0.1", 1), "headers": hops}
    assert client_address(scope, trusted) == "198.51.100.9"
    # A client on the internet cannot pick its own address.
    direct = {"client": ("203.0.113.7", 1), "headers": [(b"x-forwarded-for", b"198.51.100.9")]}
    assert client_address(direct, trusted) == "203.0.113.7"
    assert client_address(scope, ()) == "172.18.0.1"


def test_security_headers_and_hsts_over_https(tmp_path):
    with _client(tmp_path, peer="172.18.0.1") as c:
        plain = c.get("/api/v1/health/live")
        assert plain.headers["x-frame-options"] == "DENY"
        assert plain.headers["referrer-policy"] == "no-referrer"
        assert "strict-transport-security" not in plain.headers
        tls = c.get("/api/v1/health/live", headers={"X-Forwarded-Proto": "https"})
        assert tls.headers["strict-transport-security"].startswith("max-age=")
        assert c.get("/").headers["x-content-type-options"] == "nosniff"


def test_settings_read_the_guard_variables():
    s = Settings.from_env({"MIH_RATE_LIMIT": "0", "MIH_AUTH_FAILS": "5",
                           "MIH_TRUSTED_PROXIES": "10.1.0.0/16"})  # fmt: skip
    assert (s.rate_limit, s.auth_fails, s.trusted_proxies) == (0, 5, "10.1.0.0/16")
    bad = Settings.from_env({"MIH_API_TOKEN": TOKEN, "MIH_TRUSTED_PROXIES": "nope"})
    try:
        bad.validate_for_serving()
    except ConfigError as exc:
        assert "MIH_TRUSTED_PROXIES" in str(exc)
    else:
        raise AssertionError("bad MIH_TRUSTED_PROXIES accepted")
