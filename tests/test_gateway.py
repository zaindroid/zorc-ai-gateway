"""Unit tests for main.py -- no real network calls to any provider.
main.HTTP_TRANSPORT is swapped for an httpx.MockTransport per-test (see
that module's own comment on why a hand-built httpx.Response alone isn't
enough), so these never depend on (or spend) real Groq/Together/Google
quota."""
import httpx
import pytest
from fastapi.testclient import TestClient

import main


class _FakeUpstreamBody(httpx.AsyncByteStream):
    """A response built with httpx.Response(json=...) is marked
    is_stream_consumed=True the instant it's constructed (confirmed via a
    standalone probe -- true regardless of transport), so main.py's real
    proxy() -- which does client.send(req, stream=True) then
    resp.aiter_raw() to pass through both normal and SSE-streamed
    upstream responses uniformly -- can never read it. A real
    subclass of httpx.AsyncByteStream is what MockTransport's handler
    needs to return instead, to genuinely behave like an unconsumed
    streamed response the way a real network call would."""
    def __init__(self, data: bytes):
        self._data = data

    async def __aiter__(self):
        yield self._data


def _fake_response(status_code: int, body: dict) -> httpx.Response:
    import json
    return httpx.Response(status_code, headers={"content-type": "application/json"},
                           stream=_FakeUpstreamBody(json.dumps(body).encode()))


@pytest.fixture(autouse=True)
def _clean_provider_env(monkeypatch):
    for cfg in main.PROVIDERS.values():
        monkeypatch.delenv(cfg["api_key_env"], raising=False)


def test_health_never_touches_upstream():
    client = TestClient(main.app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_ready():
    client = TestClient(main.app)
    assert client.get("/ready").status_code == 200


def test_version_reports_sha(monkeypatch):
    monkeypatch.setattr(main, "BUILD_SHA", "abc1234")
    client = TestClient(main.app)
    assert client.get("/version").json()["sha"] == "abc1234"


def test_providers_reflects_which_keys_are_set(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    client = TestClient(main.app)
    status = client.get("/providers").json()
    assert status["groq"] is True
    assert status["together"] is False
    assert status["google"] is False
    assert status["xai"] is False


def test_unknown_provider_404s():
    client = TestClient(main.app)
    r = client.post("/not-a-real-provider/v1/chat/completions", json={})
    assert r.status_code == 404


def test_provider_with_no_key_returns_503_not_a_broken_request():
    client = TestClient(main.app)
    r = client.post("/groq/v1/chat/completions", json={"model": "x"})
    assert r.status_code == 503
    assert "not have" not in r.json()["detail"]  # sanity: message is coherent
    assert "no API key configured" in r.json()["detail"]


def test_configured_provider_proxies_and_injects_auth(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "sk-real-test-key")

    captured_request = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_request["url"] = str(request.url)
        captured_request["headers"] = dict(request.headers)
        return _fake_response(200, {"ok": True})

    monkeypatch.setattr(main, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    client = TestClient(main.app)
    r = client.post("/groq/v1/chat/completions", json={"model": "llama-3.3-70b", "messages": []})

    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert captured_request["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert captured_request["headers"]["authorization"] == "Bearer sk-real-test-key"
    # The real key must never leak back to the caller in any response header/body.
    assert "sk-real-test-key" not in r.text


def test_upstream_url_matches_each_providers_real_endpoint(monkeypatch):
    """Regression test: base_url + the caller's path used to double up the
    "v1" segment (".../openai/v1/v1/chat/completions"), which 404s against
    every real provider -- confirmed live the first time any provider was
    exercised with a real key (all prior tests only asserted internal
    self-consistency against a mock, never a real endpoint shape)."""
    expected = {
        "groq": "https://api.groq.com/openai/v1/chat/completions",
        "together": "https://api.together.xyz/v1/chat/completions",
        "google": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "xai": "https://api.x.ai/v1/chat/completions",
    }
    for provider, expected_url in expected.items():
        monkeypatch.setenv(main.PROVIDERS[provider]["api_key_env"], "test-key")
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return _fake_response(200, {"ok": True})

        monkeypatch.setattr(main, "HTTP_TRANSPORT", httpx.MockTransport(handler))
        client = TestClient(main.app)
        client.post(f"/{provider}/v1/chat/completions", json={})
        assert captured["url"] == expected_url


def test_caller_cannot_override_which_key_gets_used(monkeypatch):
    """A caller sending its own Authorization header must not be able to
    make the gateway use anything other than the server-held key -- the
    whole point of this proxy is that callers never handle real keys."""
    monkeypatch.setenv("GROQ_API_KEY", "sk-real-test-key")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return _fake_response(200, {"ok": True})

    monkeypatch.setattr(main, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    client = TestClient(main.app)
    client.post("/groq/v1/chat/completions", json={}, headers={"Authorization": "Bearer attacker-supplied"})

    assert captured["headers"]["authorization"] == "Bearer sk-real-test-key"
