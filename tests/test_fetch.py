import socket

import httpx
import pytest

from mih.config import Settings
from mih.sources.fetch import FetchError, check_destination, fetch_source


@pytest.fixture
def fake_dns(monkeypatch):
    table = {
        "public.example": "93.184.216.34",
        "lan.example": "192.168.1.10",
        "metadata.example": "169.254.169.254",
        "loop.example": "127.0.0.1",
    }

    def getaddrinfo(host, port, *args, **kwargs):
        if host not in table:
            raise socket.gaierror("unknown host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (table[host], port or 0))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.mark.parametrize("host", ["lan.example", "metadata.example", "loop.example"])
def test_non_public_destinations_are_blocked(fake_dns, host):
    with pytest.raises(FetchError) as exc:
        check_destination(f"http://{host}/list.m3u", allow_private=False)
    assert exc.value.code == "destination_blocked"


def test_lan_allowed_only_when_enabled_but_metadata_never(fake_dns):
    check_destination("http://lan.example/list.m3u", allow_private=True)
    with pytest.raises(FetchError):
        check_destination("http://metadata.example/latest", allow_private=True)
    with pytest.raises(FetchError):
        check_destination("http://loop.example/", allow_private=True)


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "ftp://public.example/x", "http://user:pw@public.example/x"]
)
def test_bad_urls_are_rejected(fake_dns, url):
    with pytest.raises(FetchError) as exc:
        check_destination(url, allow_private=False)
    assert exc.value.code == "validation_error"


def test_redirect_to_private_address_is_blocked(fake_dns, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://lan.example/secret"})

    with pytest.raises(FetchError) as exc:
        fetch_source("http://public.example/list.m3u", Settings(data_dir=tmp_path),
                     transport=httpx.MockTransport(handler))  # fmt: skip
    assert exc.value.code == "destination_blocked"


def test_size_limit_and_not_modified(fake_dns, tmp_path):
    settings = Settings(data_dir=tmp_path, fetch_max_bytes=10)

    def big(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 100)

    with pytest.raises(FetchError) as exc:
        fetch_source("http://public.example/a", settings, transport=httpx.MockTransport(big))
    assert exc.value.code == "too_large"

    def not_modified(request: httpx.Request) -> httpx.Response:
        assert request.headers["if-none-match"] == '"v1"'
        return httpx.Response(304)

    result = fetch_source("http://public.example/a", settings, etag='"v1"',
                          transport=httpx.MockTransport(not_modified))  # fmt: skip
    assert result.content is None


def test_a_hanging_source_is_a_fetch_error(fake_dns, tmp_path):
    def hang(request):
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(FetchError) as exc:
        fetch_source("http://public.example/guide.xml.gz", Settings(data_dir=tmp_path),
                     transport=httpx.MockTransport(hang))  # fmt: skip
    assert exc.value.code == "network" and "ReadTimeout" in str(exc.value)
