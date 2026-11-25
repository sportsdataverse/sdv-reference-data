"""espn.get_json: a lapsed proxy account (Decodo 407, 2026-11-02 and 2026-11-23) must not fail every league's fetch."""

import requests

from sdv_reference import espn


class _Resp:
    status_code = 200

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {"ok": True}


def test_an_unreachable_proxy_falls_back_to_direct_for_the_run(monkeypatch):
    monkeypatch.setenv("SDV_API_PROXY", "http://user:pass@proxy.invalid:7000")
    monkeypatch.setattr(espn, "_PROXY_DOWN", [])
    calls = []

    class Session:
        def get(self, url, proxies=None, **_):
            calls.append(proxies)
            if proxies:
                raise requests.exceptions.ProxyError(
                    "Tunnel connection failed: 407 Proxy Authentication Required"
                )
            return _Resp()

    assert espn.get_json("https://site.api.espn.com/a", Session(), pause=0) == {
        "ok": True
    }
    assert espn.get_json("https://site.api.espn.com/b", Session(), pause=0) == {
        "ok": True
    }
    assert [bool(p) for p in calls] == [
        True,
        False,
        False,
    ]  # one failed proxied call, then direct


def test_a_direct_failure_still_raises(monkeypatch):
    monkeypatch.delenv("SDV_API_PROXY", raising=False)
    monkeypatch.setattr(espn, "_PROXY_DOWN", [])
    monkeypatch.setattr(espn.time, "sleep", lambda _s: None)

    class Session:
        def get(self, url, proxies=None, **_):
            raise requests.exceptions.ConnectionError("down")

    try:
        espn.get_json("https://site.api.espn.com/a", Session(), pause=0)
    except requests.exceptions.ConnectionError:
        return
    raise AssertionError("a direct connection failure must raise")
