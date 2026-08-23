# zorc-ai-gateway

Internal OpenAI-compatible reverse proxy to free-tier AI providers
(Groq, Together AI, Google AI Studio, OpenRouter). Holds the real
provider API keys so no other app on the zorc platform ever needs to see
or handle one directly. Also does smart auto-failover across them --
see "Auto-routing" below.

xAI (Grok) was tried and deliberately removed -- pay-as-you-go only, no
free tier, doesn't fit this gateway's "genuinely free" purpose. Groq (a
different company, confusingly similar name) is the one that's actually
free -- see "Recommended models" below.

## How it works

An app calls this gateway exactly like it would call OpenAI's API
directly, except the base URL is this gateway plus the provider it
wants:

```
http://<gateway internal address>:8080/groq/v1/chat/completions
http://<gateway internal address>:8080/together/v1/chat/completions
http://<gateway internal address>:8080/google/v1/chat/completions
http://<gateway internal address>:8080/openrouter/v1/chat/completions
```

The gateway injects the real `Authorization` header for that provider
and forwards the request (and the response, including SSE streaming for
`stream: true` chat completions) unmodified otherwise. Any
`Authorization` header a caller sends is ignored -- the gateway always
uses its own server-held key, never a caller-supplied one.

## Recommended models (Groq)

Confirmed live against the real Groq catalog on 2026-08-23
(`GET /groq/v1/models` through the gateway itself -- always check that
endpoint for the current list, Groq's free catalog changes over time):

- **`openai/gpt-oss-120b`** -- best general-purpose default. Large
  context (131k), tool calling, JSON mode, structured outputs, and
  reasoning, all free. It's a *reasoning* model: it spends part of
  `max_tokens` on an internal `reasoning` field before writing the
  visible answer, so a very small `max_tokens` (e.g. 10) can come back
  empty -- confirmed live. Give it real headroom (200+) for actual use.
- **`qwen/qwen3.6-27b`** -- lighter/faster alternative, same feature set
  (tools, JSON mode, reasoning) plus image input, smaller max output
  (16k vs 65k) but same 131k context. Also a thinking model -- confirmed
  live it emits its `<think>...</think>` reasoning inline in `content`
  (not a separate field the way gpt-oss's `reasoning` field is), so give
  it real `max_tokens` headroom too or the visible answer gets cut off
  mid-thought.
- **`groq/compound`** / **`groq/compound-mini`** -- Groq's own agentic
  models (built-in web search / code execution), useful when an app
  needs an agent loop rather than a plain chat completion.

Whisper (`whisper-large-v3`, `whisper-large-v3-turbo`) is available too,
for audio transcription rather than chat.

## Auto-routing (POST /auto/v1/chat/completions)

For a caller that just wants a free chat completion and doesn't care
which provider serves it: point your OpenAI client's base_url at
`.../auto/v1` instead of `.../groq/v1` etc. The gateway tries providers
in `ROTATION_ORDER` (currently `groq -> google -> openrouter -> together`,
ordered by how generous/reliable each has actually proven live), skipping
any that has no key configured, is rate-limited right now (self-imposed
or its own quota), or is in a short cooldown from a recent failure.

- Whatever `model` you send is **ignored** -- each candidate substitutes
  its own `default_model` from `PROVIDERS`. If you care about an exact
  model, call that provider's direct route instead; /auto is for "just
  give me a free completion."
- Only genuine availability signals trigger moving to the next candidate:
  a `429`/`402`/`403` from the upstream, or a network-level failure.
  An ordinary error (bad request, unsupported param) is returned as-is
  immediately -- failing over on that would silently hide a real bug.
- A provider that just failed is skipped (not retried) for
  `COOLDOWN_SECONDS` (120s) -- so a struggling provider doesn't get
  hammered by every subsequent /auto call while it's down.
- A successful response carries `X-Gateway-Provider: <name>` so you can
  see which one actually served it.
- `GET /auto/status` reports live eligibility (`has_key`, `cooling_down`,
  `rate_limit_available`, `eligible`) for every candidate, in rotation
  order -- the "keep track of which provider is available" piece.
- If every candidate is unavailable, you get one clean `503` listing what
  was tried and the full status of each candidate -- not a silent hang or
  a confusing upstream error from whichever one happened to be tried last.

Direct `/{provider}/...` routes are completely unaffected by any of this
-- they always attempt the real call with the model you actually sent,
exactly as before /auto existed.

## Trust boundary

**This app is never publicly reachable.** It's deployed via zorc's normal
`deploy()` (which always creates a public DNS/tunnel route -- there's no
"internal-only" option yet), and that public route is removed manually
immediately after the first deploy. Reachable only over the platform's
internal Docker network from then on -- the same trust model this
platform's shared Redis already uses (network reachability is the
boundary, not a per-caller token). If this app is ever redeployed from
scratch, that public-route removal step needs to be redone.

## Provider keys

Deliberately NOT declared in `app.yaml`'s `env:` section (that would
either auto-generate them, which makes no sense for a real third-party
key, or mark them `required: true`, which would block deployment until
all three are ready). Instead:

- The app reads `GROQ_API_KEY` / `TOGETHER_API_KEY` /
  `GOOGLE_AI_STUDIO_API_KEY` / `OPENROUTER_API_KEY` from the environment
  directly, treating an unset one as "not configured yet" -- that
  provider's routes return a clean `503`, every other provider keeps
  working (and /auto just skips it).
- `GET /providers` reports which ones currently have a real key set.
- Wiring a key in is `set_coolify_env_vars(coolify_uuid, {KEY: value})`
  followed by `redeploy()` -- a plain restart does NOT pick up a new env
  var, the container has to be recreated (same rule as a memory-limit
  change). Not yet a standalone zorc tool.
- `GROQ_API_KEY`, `GOOGLE_AI_STUDIO_API_KEY`, and `OPENROUTER_API_KEY`
  are all wired and confirmed working end-to-end (real chat completions
  through this gateway, 2026-08-23). `TOGETHER_API_KEY` is not set yet
  (still a valid /auto candidate, it's just skipped until it has a key).

## Adding a provider

Add one entry to `PROVIDERS` in `main.py` (upstream base URL, the env var
name for its key, how the key gets attached to the request) -- no other
code changes needed, the proxy route itself is entirely table-driven.

## OpenRouter: models + free-tier enforcement

`nvidia/nemotron-3-super-120b-a12b:free` is the default -- confirmed
live 2026-08-23 (real "pong" back, `cost: 0`). An earlier choice here,
`meta-llama/llama-3.3-70b-instruct:free`, was already pulled from the
free tier by the time it got tested live -- OpenRouter's free catalog
rotates fast, always confirm via `GET /openrouter/v1/models` (filter for
`":free"` model ids) rather than trusting this doc. Like `google`,
it's rate-limited by this gateway (`rpm_limit`/`rpd_limit` on the
`openrouter` entry in `PROVIDERS`, defaults 20/min & 50/day --
OpenRouter's own documented unfunded-account limits, raise
`OPENROUTER_RPD_LIMIT` to 1000 if this key's account has ever purchased
$10+ in credits). Free-model requests cost $0 even unfunded -- no
billing-account guarantee needed here the way Google's is, OpenRouter
just rejects with a quota error past the free limit.

## Google AI Studio: models + free-tier enforcement

Confirmed live 2026-08-23: `gemini-2.5-flash` is no longer available to
new users/keys -- Google's own error message says to use
`gemini-3.6-flash` instead. Like the Groq reasoning models above, it can
burn `max_tokens` on internal thinking (`extra_content.google.thought_signature`)
before any visible `content` -- give it real headroom (200+) or you get
`finish_reason: "length"` with an empty message, confirmed live.

`google` is the one provider where staying free actually matters (Groq's
free tier is generous and unlimited-in-practice for this use case;
Together isn't wired yet) -- so it's the only provider with a
self-imposed rate limit, two layers deep:

1. **The real guarantee**: per Google's own docs
   (ai.google.dev/gemini-api/docs/billing), an API key whose underlying
   Cloud project has no linked Cloud Billing account cannot be charged,
   full stop -- exceeding free quota there only ever produces a 429, no
   bill. Confirm billing is NOT linked on this key's project; that's what
   actually makes overspend structurally impossible, not anything in this
   repo.
2. **This gateway's own limiter**, as a second layer so a runaway caller
   gets a clean local cutoff instead of hammering Google with requests
   that would fail anyway: `rpm_limit`/`rpd_limit` on the `google` entry
   in `PROVIDERS` (defaults 8/min, 200/day -- deliberately conservative,
   configurable via `GOOGLE_RPM_LIMIT`/`GOOGLE_RPD_LIMIT` env vars). Once
   hit, the gateway returns `429` itself and never forwards the request.
   `GET /usage` reports current usage against both limits.

Check your actual current limit at
[aistudio.google.com/rate-limit](https://aistudio.google.com/rate-limit)
and raise the env vars if 8/200 is too strict for your model/tier.

## Build: nixpacks, not a Dockerfile

This repo has `requirements.txt` at its root, which zorc's `classify()`
treats as a recognized manifest -- per this platform's own convention
(AGENTS.md §3: "Dockerfile only if build-autodetection can't handle your
stack"), that means Coolify builds this app via nixpacks auto-detection,
NOT a hand-written Dockerfile, even if one existed in the repo. A real,
hand-written Dockerfile was tried first and silently ignored (confirmed
live: classify() picks the manifest over Dockerfile whenever both exist,
so it was always going to be dead code) -- removed rather than left
around as confusing, unused weight.

`Procfile`'s `web:` line is what tells nixpacks how to actually start
this app (`uvicorn main:app --host 0.0.0.0 --port $PORT`) -- without it,
nixpacks has no reliable way to guess that a FastAPI app should be
started with uvicorn, and the container exits immediately on deploy
(confirmed live: this exact failure, before the Procfile was added).
