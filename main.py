"""zorc-ai-gateway -- internal OpenAI-compatible reverse proxy to free-tier
AI providers (Groq, Together AI, Google AI Studio). Holds the real
provider API keys so no other app on this platform ever needs to see or
handle one directly -- an app points its OpenAI client's base_url at this
gateway's internal address plus the provider it wants
(http://<gateway>:8080/groq/v1, /together/v1, /google/v1) and gets
transparent proxying + auth injection, streaming included.

Never exposed outside the platform's internal Docker network -- no public
domain, no Cloudflare Tunnel route (removed manually after the initial
deploy, since zorc's deploy() has no "internal-only" concept yet).
Internal-network-only IS the trust boundary here, same model this
platform's own shared Redis already uses (no per-caller token) -- not
weaker than existing convention, consistent with it.

A provider with no key configured yet (deliberate -- this ships before
real keys are wired in, see README) returns a clear 503, never a silent
failure or a request forwarded with a missing/empty Authorization header.
"""
import os

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
        # Gemini's documented OpenAI-compatibility layer -- NOT yet
        # verified against a real key/live request (no key wired in as
        # of this writing). Confirm this exact base path works once
        # GOOGLE_AI_STUDIO_API_KEY is set; adjust here if not.
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "api_key_env": "GOOGLE_AI_STUDIO_API_KEY",
        "auth_header": lambda key: {"Authorization": f"Bearer {key}"},
    },
    "xai": {
        # Grok, OpenAI-compatible API.
        "base_url": "https://api.x.ai/v1",
        "api_key_env": "XAI_API_KEY",
        "auth_header": lambda key: {"Authorization": f"Bearer {key}"},
    },
}


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

    upstream_url = f"{cfg['base_url']}/{path}"
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
