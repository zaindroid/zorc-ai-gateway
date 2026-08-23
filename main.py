"""zorc-ai-gateway -- internal OpenAI-compatible reverse proxy to free-tier
AI providers (Groq, Together AI, Google AI Studio, OpenRouter). Holds the
real provider API keys so no other app on this platform ever needs to see
or handle one directly -- an app points its OpenAI client's base_url at
this gateway's internal address plus the provider it wants
(http://<gateway>:8080/groq/v1, /together/v1, /google/v1, /openrouter/v1)
and gets transparent proxying + auth injection, streaming included.

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
(google and openrouter do, as of 2026-08-23) -- self-imposed request-rate
caps the gateway enforces BEFORE ever forwarding to the upstream, on top
of whatever quota that provider enforces on its own. See README for the
two-layer reasoning behind this (the real guarantee is the provider's own
"no linked billing = literally cannot be charged"; this is a second,
defense-in-depth layer, not a replacement for it).

## Auto-routing (POST /auto/v1/chat/completions)

Every direct /{provider}/... route stays exactly as before -- explicit,
caller-picked, model id passed straight through. /auto/v1/chat/completions
is a separate, additive endpoint for a caller that just wants "give me a
free chat completion, I don't care which provider": it ignores whatever
`model` the caller sent and substitutes each candidate provider's own
`default_model` in ROTATION_ORDER, trying them in order and skipping any
that has no key configured, is in a temporary failure cooldown, or has no
self-imposed rate-limit budget left right now. It only advances to the
next candidate on signals that mean "this provider is genuinely
unavailable right now" (429/402/403 from the upstream, or a network-level
failure) -- never on an ordinary 4xx like a bad request, which almost
certainly means a caller/config bug that failing over would only hide.
`GET /auto/status` reports live eligibility per candidate; a successful
/auto response carries `X-Gateway-Provider` naming whichever one actually
served it.
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
# holding its API key, how the key gets attached to the outgoing request,
# and (optional) a default_model used only by /auto -- direct /{provider}/
# calls always take the model straight from the caller, unchanged. Adding
# a new provider is exactly one more entry here -- no other code changes
# needed, both routing modes are entirely table-driven.
PROVIDERS = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "api_key_env": "GROQ_API_KEY",
        "auth_header": lambda key: {"Authorization": f"Bearer {key}"},
        # Confirmed live 2026-08-23 -- best general free default (131k
        # context, tools, JSON mode, reasoning). It's a thinking model:
        # give it real max_tokens headroom or it burns the budget on
        # internal reasoning before writing a visible answer.
        "default_model": "openai/gpt-oss-120b",
    },
    "together": {
        "base_url": "https://api.together.xyz/v1",
        "api_key_env": "TOGETHER_API_KEY",
        "auth_header": lambda key: {"Authorization": f"Bearer {key}"},
        # NOT verified live -- no TOGETHER_API_KEY configured yet. Confirm
        # this is still a real, free-tier "Free"-tagged Together model
        # once a key is wired in, and adjust if not; harmless either way
        # until then since no key means /auto skips this candidate.
        "default_model": "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free",
    },
    "google": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "api_key_env": "GOOGLE_AI_STUDIO_API_KEY",
        "auth_header": lambda key: {"Authorization": f"Bearer {key}"},
        # Confirmed live 2026-08-23: gemini-2.5-flash is 404 for new
        # keys, Google's own error says use gemini-3.6-flash instead.
        # Also a thinking model (reasoning burned before visible content,
        # exposed via extra_content.google.thought_signature) -- same
        # max_tokens-headroom rule as groq's default above.
        "default_model": "gemini-3.6-flash",
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
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "auth_header": lambda key: {"Authorization": f"Bearer {key}"},
        # OpenRouter's free (":free"-suffixed) models -- confirm this
        # specific one is still live via GET /openrouter/v1/models, its
        # free catalog rotates. Free-model requests are always $0 even on
        # an unfunded account (no billing needed at all), so the only
        # real constraint here is the rate limit below, not spend risk.
        "default_model": "meta-llama/llama-3.3-70b-instruct:free",
        # OpenRouter's documented free-tier limits for an unfunded
        # account: 20 req/min (shared across all :free models, does not
        # change with credit balance) and 50 req/day (rises to 1000/day
        # once the account has ever purchased $10+ in credits -- raise
        # OPENROUTER_RPD_LIMIT if that's true for this key).
        "rpm_limit": int(os.environ.get("OPENROUTER_RPM_LIMIT", "20")),
        "rpd_limit": int(os.environ.get("OPENROUTER_RPD_LIMIT", "50")),
    },
}

# The order /auto tries candidates in -- deliberately hand-ordered by how
# reliable each has actually proven to be in this gateway's own live
# testing, not alphabetical or insertion order: groq's free tier is by
# far the most generous (no self-imposed limit at all), google and
# openrouter are both real but tightly capped, together is untested.
ROTATION_ORDER = ["groq", "google", "openrouter", "together"]

# How long a provider that just failed with a quota/availability signal
# (429/402/403, or a network-level error reaching it) is skipped by /auto
# without being retried -- keeps a struggling provider from being probed
# on every single /auto call once it's already told us it's unavailable.
# Direct /{provider}/... calls ignore this entirely; only /auto consults
# it, since only /auto is choosing on the caller's behalf.
COOLDOWN_SECONDS = 120

# provider name -> deque[float] of unix timestamps for calls the gateway
# has allowed through, used to enforce rpm_limit/rpd_limit above. In-memory
# only (this app runs as a single replica, see app.yaml) -- resets on
# restart/redeploy, which is fine: losing a partial day's count on a rare
# restart is a much smaller risk than the thing being guarded against.
_usage: dict[str, deque[float]] = {}

# provider name -> unix timestamp until which /auto treats it as
# unavailable, set by _mark_cooldown() below. Also in-memory-only, same
# reasoning as _usage.
_cooldown_until: dict[str, float] = {}

# Status codes that mean "this provider itself is the problem right now"
# (quota/credits/auth-tier exhausted) rather than "this specific request
# was bad" -- confirmed live as the actual codes each of xAI (403, before
# it was removed) and rate-limited providers return. Only these trigger
# /auto moving to the next candidate; any other 4xx is returned as-is,
# since retrying elsewhere would silently hide a real bug instead of
# surfacing it.
_FAILOVER_STATUS_CODES = {402, 403, 429}


def _rate_limit_remaining(provider: str, cfg: dict) -> bool:
    """Peek-only version of the rpm/rpd check -- true if calling this
    provider right now would NOT be rejected by its self-imposed limit.
    Does not reserve a slot; used by /auto to decide whether a candidate
    is even worth attempting before it commits to trying it."""
    rpm_limit = cfg.get("rpm_limit")
    rpd_limit = cfg.get("rpd_limit")
    if not rpm_limit and not rpd_limit:
        return True

    now = time.time()
    dq = _usage.get(provider, deque())
    if rpd_limit and sum(1 for t in dq if t > now - 86400) >= rpd_limit:
        return False
    if rpm_limit and sum(1 for t in dq if t > now - 60) >= rpm_limit:
        return False
    return True


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


def _mark_cooldown(provider: str) -> None:
    _cooldown_until[provider] = time.time() + COOLDOWN_SECONDS


def _in_cooldown(provider: str) -> bool:
    return time.time() < _cooldown_until.get(provider, 0)


def _auto_candidate_status(provider: str) -> dict:
    cfg = PROVIDERS[provider]
    has_key = bool(os.environ.get(cfg["api_key_env"]))
    cooling_down = _in_cooldown(provider)
    rate_ok = _rate_limit_remaining(provider, cfg)
    return {
        "has_key": has_key,
        "cooling_down": cooling_down,
        "cooldown_remaining_s": max(0, round(_cooldown_until.get(provider, 0) - time.time())) if cooling_down else 0,
        "rate_limit_available": rate_ok,
        "eligible": has_key and not cooling_down and rate_ok,
    }


def provider_status() -> dict[str, bool]:
    return {name: bool(os.environ.get(cfg["api_key_env"])) for name, cfg in PROVIDERS.items()}


def _forward_path(path: str) -> str:
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
    return path[3:] if path.startswith("v1/") else path


async def _send_upstream(client: httpx.AsyncClient, cfg: dict, key: str, method: str, path: str,
                          headers: dict, params: dict, body: bytes) -> httpx.Response:
    # Takes an already-created client rather than making its own -- the
    # caller owns the client's lifetime (creates it, passes it here, then
    # closes it together with the response it gets back, see
    # _stream_response's finally block / the explicit aclose() pairs on
    # the failover path below). A version of this that built its own
    # client internally would silently orphan it -- the caller's own
    # client would never actually send anything, and the real one
    # wouldn't get closed -- a real bug caught in review before this
    # shipped, not from a live symptom.
    upstream_url = f"{cfg['base_url']}/{_forward_path(path)}"
    req_headers = dict(cfg["auth_header"](key))
    req_headers.update(headers)
    req = client.build_request(method, upstream_url, headers=req_headers, params=params, content=body)
    return await client.send(req, stream=True)


def _stream_response(client: httpx.AsyncClient, upstream_resp: httpx.Response,
                      extra_headers: dict | None = None) -> StreamingResponse:
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
        headers=extra_headers,
    )


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


@app.get("/auto/status")
async def auto_status():
    """Live eligibility of every /auto candidate, in the order /auto
    actually tries them -- the "keep track of which provider is
    available" half of the smart-routing feature."""
    return {name: _auto_candidate_status(name) for name in ROTATION_ORDER}


@app.get("/openapi.json")
async def openapi_json():
    return app.openapi()


@app.api_route("/{provider}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(provider: str, path: str, request: Request):
    if provider == "auto":
        return await auto_proxy(path, request)

    cfg = PROVIDERS.get(provider)
    if cfg is None:
        raise HTTPException(404, f"unknown provider {provider!r} -- available: {sorted(PROVIDERS)}")

    key = os.environ.get(cfg["api_key_env"])
    if not key:
        raise HTTPException(503, f"provider {provider!r} has no API key configured yet -- "
                                   "see GET /providers for current status")

    _check_and_reserve_rate_limit(provider, cfg)

    headers = {}
    if "content-type" in request.headers:
        headers["Content-Type"] = request.headers["content-type"]
    body = await request.body()

    client = httpx.AsyncClient(timeout=120, transport=HTTP_TRANSPORT)
    upstream_resp = await _send_upstream(client, cfg, key, request.method, path, headers,
                                          dict(request.query_params), body)
    if upstream_resp.status_code in _FAILOVER_STATUS_CODES:
        _mark_cooldown(provider)
    return _stream_response(client, upstream_resp)


async def auto_proxy(path: str, request: Request):
    """Handler for /auto/... -- tries ROTATION_ORDER in turn, skipping any
    candidate that isn't eligible right now (see _auto_candidate_status),
    substituting each candidate's own default_model for whatever `model`
    the caller sent (see module docstring for why). Advances to the next
    candidate only on a real availability signal (_FAILOVER_STATUS_CODES
    or a network-level error); any other response -- success or a genuine
    caller/config error -- is returned immediately as-is."""
    body = await request.body()
    import json as _json
    try:
        payload = _json.loads(body) if body else {}
    except ValueError:
        payload = None  # not JSON -- forwarded as-is, no model substitution possible

    tried = []
    for provider in ROTATION_ORDER:
        cfg = PROVIDERS[provider]
        key = os.environ.get(cfg["api_key_env"])
        if not key or _in_cooldown(provider) or not _rate_limit_remaining(provider, cfg):
            continue

        _check_and_reserve_rate_limit(provider, cfg)
        tried.append(provider)

        if body and payload is not None and "default_model" in cfg:
            request_body = _json.dumps({**payload, "model": cfg["default_model"]}).encode()
        else:
            request_body = body

        headers = {"Content-Type": "application/json"}
        client = httpx.AsyncClient(timeout=120, transport=HTTP_TRANSPORT)
        try:
            upstream_resp = await _send_upstream(client, cfg, key, request.method, path, headers,
                                                   dict(request.query_params), request_body)
        except httpx.RequestError:
            await client.aclose()
            _mark_cooldown(provider)
            continue

        if upstream_resp.status_code in _FAILOVER_STATUS_CODES:
            await upstream_resp.aclose()
            await client.aclose()
            _mark_cooldown(provider)
            continue

        return _stream_response(client, upstream_resp, extra_headers={"X-Gateway-Provider": provider})

    raise HTTPException(503, {
        "message": "no provider available right now",
        "attempted_and_failed": tried,
        "status": {name: _auto_candidate_status(name) for name in ROTATION_ORDER},
    })
