"""espn.get_json: a lapsed proxy account (Decodo 407, 2026-11-02 and 2026-11-23) must not fail every league's fetch."""

import pytest
import requests

from sdv_reference import espn


class _Resp:
    status_code = 200

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {"ok": True}


@pytest.fixture
def proxied(monkeypatch):
    monkeypatch.setenv("SDV_API_PROXY", "http://user:pass@proxy.invalid:7000")
    monkeypatch.setattr(espn, "_PROXY_DOWN", [])
    monkeypatch.setattr(espn.time, "sleep", lambda _s: None)


def _session(calls: list, proxy_errors: list[str]):
    """A session whose proxied calls raise the given errors in turn (then succeed); direct calls succeed."""

    class Session:
        def get(self, url, proxies=None, **_):
            calls.append(bool(proxies))
            if proxies and proxy_errors:
                raise requests.exceptions.ProxyError(proxy_errors.pop(0))
            return _Resp()

    return Session()


def test_a_refused_proxy_account_goes_direct_for_the_rest_of_the_fetch(proxied):
    calls: list[bool] = []
    s = _session(calls, ["Tunnel connection failed: 407 Proxy Authentication Required"])
    assert espn.get_json("https://site.api.espn.com/a", s, pause=0) == {"ok": True}
    assert espn.get_json("https://site.api.espn.com/b", s, pause=0) == {"ok": True}
    assert calls == [True, False, False]  # one refused proxied call, then direct


def test_a_passing_proxy_hiccup_is_retried_through_the_proxy(proxied):
    calls: list[bool] = []
    s = _session(calls, ["Unable to connect to proxy: reset"])
    assert espn.get_json("https://site.api.espn.com/a", s, pause=0) == {"ok": True}
    assert calls == [True, True]
    assert not espn._PROXY_DOWN


def test_a_proxy_that_never_recovers_still_gets_a_direct_call(proxied):
    calls: list[bool] = []
    s = _session(calls, ["Unable to connect to proxy: reset"] * 4)
    assert espn.get_json("https://site.api.espn.com/a", s, pause=0) == {"ok": True}
    assert calls == [True, True, True, True, False]


def test_a_direct_failure_still_raises(monkeypatch):
    monkeypatch.delenv("SDV_API_PROXY", raising=False)
    monkeypatch.setattr(espn, "_PROXY_DOWN", [])
    monkeypatch.setattr(espn.time, "sleep", lambda _s: None)

    class Session:
        def get(self, url, proxies=None, **_):
            raise requests.exceptions.ProxyError(
                "a proxy from the environment, not ours"
            )

    with pytest.raises(requests.exceptions.ProxyError):
        espn.get_json("https://site.api.espn.com/a", Session(), pause=0)
