"""zorc-ai-gateway -- internal OpenAI-compatible reverse proxy to free-tier
AI providers (Groq, Together AI, Google AI Studio). Holds the real
provider API keys so no other app on this platform ever needs to see or
handle one directly -- an app points its OpenAI client's base_url at this
gateway's internal address plus the provider it wants
(http://<gateway>:8080/groq/v1, /together/v1, /google/v1) and gets
transparent proxying + auth injection, streaming included.

xAI (Grok) was tried and deliberately removed -- it has no free tier,
pay-as-you-go credits required at console.x.ai, doesn't fit this
platform's "genuinely free" ideology for auxiliary AI compute. Groq (a
different company, confusingly similar name) is the one that's actually
free -- see README for recommended models.

Never exposed outside the platform's internal Docker network -- no public
domain, no Cloudflare Tunnel route (removed manually after the initial
deploy, since zorc's deploy() has no "internal-only" concept yet).
Internal-network-only IS the trust boundary here, same model this
platform's own shared Redis already uses (no per-caller token) -- not
weaker than existing convention, consistent with it.

A provider with no key configured yet (deliberate -- this ships before
real keys are wired in, see README) returns a clear 503, never a silent
failure or a request forwarded with a missing/empty Authorization header.

A provider can optionally declare rpm_limit/rpd_limit in PROVIDERS below
(google does, as of 2026-08-23) -- self-imposed request-rate caps the
gateway enforces BEFORE ever forwarding to the upstream, on top of
whatever quota that provider enforces on its own. This is deliberately
a second, independent layer, not a replacement for the real guarantee:
per Google's own docs (ai.google.dev/gemini-api/docs/billing), an API
key whose underlying Cloud project has no linked billing account cannot
be charged at all -- exceeding free quota there only ever produces a 429,
never a bill. Confirm that's still true for this specific key's project
before relying on it. This gateway's own limiter exists so a runaway
caller gets a clean, immediate, self-hosted cutoff instead of spamming
Google with requests that would fail anyway once its own quota is hit.
"""
import os
import time
from collections import deque

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

BUILD_SHA = os.environ.get("GIT_SHA", "dev")

app = FastAPI(title="zorc-ai-gateway", docs_url=None, redoc_url=None)

# None in production -- httpx's real network transport. Tests override
# this with an httpx.MockTransport so a fake upstream response goes
# through httpx's own genuine streaming machinery (see proxy()'s comment
# on why a hand-built httpx.Response doesn't work for that).
HTTP_TRANSPORT: httpx.AsyncBaseTransport | None = None

# One entry per supported provider: real upstream base URL, the env var
# holding its API key, and how that key gets attached to the outgoing
# request. Adding a new provider is exactly one more entry here -- no
# other code changes needed, routing is entirely table-driven.
PROVIDERS = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "api_key_env": "GROQ_API_KEY",
        "auth_header": lambda key: {"Authorization": f"Bearer {key}"},
    },
    "together": {
        "base_url": "https://api.together.xyz/v1",
        "api_key_env": "TOGETHER_API_KEY",
        "auth_header": lambda key: {"Authorization": f"Bearer {key}"},
    },
    "google": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "api_key_env": "GOOGLE_AI_STUDIO_API_KEY",
        "auth_header": lambda key: {"Authorization": f"Bearer {key}"},
        # Deliberately conservative, well under every published free-tier
        # number for Gemini's flash-family models (which range roughly
        # 10-15 RPM / 200-1500 RPD depending on model and change over
        # time) -- the goal here is a comfortable safety margin, not a
        # tight match to Google's exact current limit. Check your real
        # limit at https://aistudio.google.com/rate-limit and raise these
        # via GOOGLE_RPM_LIMIT/GOOGLE_RPD_LIMIT if 8/200 is too strict.
        "rpm_limit": int(os.environ.get("GOOGLE_RPM_LIMIT", "8")),
        "rpd_limit": int(os.environ.get("GOOGLE_RPD_LIMIT", "200")),
    },
}

# provider name -> deque[float] of unix timestamps for calls the gateway
# has allowed through, used to enforce rpm_limit/rpd_limit above. In-memory
# only (this app runs as a single replica, see app.yaml) -- resets on
# restart/redeploy, which is fine: losing a partial day's count on a rare
# restart is a much smaller risk than the thing being guarded against.
_usage: dict[str, deque[float]] = {}


def _check_and_reserve_rate_limit(provider: str, cfg: dict) -> None:
    """Raises 429 if this call would exceed provider's rpm_limit/rpd_limit;
    otherwise reserves the slot immediately (counts it right away, not
    after the upstream call succeeds) -- simplest correct behavior for a
    single-process gateway with no concurrency to race against, and it
    means a burst of calls right at the boundary can only ever undercount
    (a call that fails upstream for an unrelated reason still "spent" its
    reserved slot) rather than overshoot the cap."""
    rpm_limit = cfg.get("rpm_limit")
    rpd_limit = cfg.get("rpd_limit")
    if not rpm_limit and not rpd_limit:
        return

    now = time.time()
    dq = _usage.setdefault(provider, deque())
    while dq and dq[0] < now - 86400:
        dq.popleft()

    if rpd_limit and len(dq) >= rpd_limit:
        raise HTTPException(429, f"{provider!r} daily free-tier budget exhausted "
                                   f"({rpd_limit}/day, self-imposed) -- see GET /usage; "
                                   "resets on a rolling 24h window")

    if rpm_limit:
        recent = sum(1 for t in dq if t > now - 60)
        if recent >= rpm_limit:
            raise HTTPException(429, f"{provider!r} rate limit hit "
                                       f"({rpm_limit}/min, self-imposed) -- see GET /usage; "
                                       "retry in a few seconds")

    dq.append(now)


def provider_status() -> dict[str, bool]:
    return {name: bool(os.environ.get(cfg["api_key_env"])) for name, cfg in PROVIDERS.items()}


@app.get("/health")
async def health():
    # Must not touch any upstream provider -- a slow/down provider must
    # never make this gateway itself look unhealthy to the platform.
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    return {"status": "ready"}


@app.get("/version")
async def version():
    return {"sha": BUILD_SHA, "built": None}


@app.get("/providers")
async def providers():
    """Which providers have a real key configured right now -- lets a
    caller (or a human) check availability before relying on one, and is
    how you confirm a newly-wired-in key actually took effect."""
    return provider_status()


@app.get("/usage")
async def usage():
    """Current self-imposed rate-limit usage for every provider that
    declares rpm_limit/rpd_limit in PROVIDERS -- the "strictly monitor"
    half of free-tier enforcement (the other half is this gateway
    refusing to forward once a limit is hit, see proxy())."""
    now = time.time()
    result = {}
    for name, cfg in PROVIDERS.items():
        rpm_limit = cfg.get("rpm_limit")
        rpd_limit = cfg.get("rpd_limit")
        if not rpm_limit and not rpd_limit:
            continue
        dq = _usage.get(name, deque())
        result[name] = {
            "rpm_used": sum(1 for t in dq if t > now - 60),
            "rpm_limit": rpm_limit,
            "rpd_used": sum(1 for t in dq if t > now - 86400),
            "rpd_limit": rpd_limit,
        }
    return result


@app.get("/openapi.json")
async def openapi_json():
    return app.openapi()


@app.api_route("/{provider}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(provider: str, path: str, request: Request):
    cfg = PROVIDERS.get(provider)
    if cfg is None:
        raise HTTPException(404, f"unknown provider {provider!r} -- available: {sorted(PROVIDERS)}")

    key = os.environ.get(cfg["api_key_env"])
    if not key:
        raise HTTPException(503, f"provider {provider!r} has no API key configured yet -- "
                                   "see GET /providers for current status")

    _check_and_reserve_rate_limit(provider, cfg)

    # Every base_url above already ends in that provider's own real API
    # root (including its version marker, e.g. ".../openai/v1" for groq,
    # ".../v1beta/openai" for google) -- and a caller pointing an
    # OpenAI-SDK-style client's base_url at this gateway (base_url =
    # "http://gateway/<provider>/v1", matching openai-python's own
    # convention of baking "/v1" into base_url rather than adding it per
    # call) will always have "v1/..." as the leading segment of `path`
    # too. Concatenating both unmodified doubles the version segment
    # (".../openai/v1/v1/chat/completions"), which 404s against the real
    # upstream -- confirmed live against the real xAI API the first time
    # any provider here was ever exercised end-to-end with a real key
    # (every prior test used a mock and only asserted internal
    # self-consistency, never a real upstream). Stripping exactly one
    # leading "v1/" segment here is what makes the constructed URL match
    # each provider's actual documented endpoint.
    forward_path = path[3:] if path.startswith("v1/") else path
    upstream_url = f"{cfg['base_url']}/{forward_path}"
    headers = dict(cfg["auth_header"](key))
    if "content-type" in request.headers:
        headers["Content-Type"] = request.headers["content-type"]

    body = await request.body()
    # transport=HTTP_TRANSPORT is None in production (httpx's real network
    # transport); tests override it with httpx.MockTransport so the fake
    # upstream response goes through httpx's own real response-streaming
    # machinery -- a hand-built httpx.Response(json=...) does NOT support
    # aiter_raw() the same way a genuine network/transport response does
    # (confirmed live: raises StreamConsumed), so this is the actually-
    # correct way to fake an upstream call for this handler, not a
    # production workaround for a test-only problem.
    client = httpx.AsyncClient(timeout=120, transport=HTTP_TRANSPORT)
    req = client.build_request(request.method, upstream_url, headers=headers,
                                params=dict(request.query_params), content=body)
    upstream_resp = await client.send(req, stream=True)

    # Forwards the raw byte stream unmodified either way -- a normal JSON
    # response arrives as one chunk, an SSE stream (chat completions with
    # stream: true) arrives incrementally; StreamingResponse handles both
    # the same way, so there's no separate non-streaming code path to
    # keep in sync with this one.
    async def _stream_and_close():
        try:
            async for chunk in upstream_resp.aiter_raw():
                yield chunk
        finally:
            await upstream_resp.aclose()
            await client.aclose()

    return StreamingResponse(
        _stream_and_close(),
        status_code=upstream_resp.status_code,
        media_type=upstream_resp.headers.get("content-type"),
    )
