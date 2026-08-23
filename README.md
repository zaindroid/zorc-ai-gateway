# zorc-ai-gateway

Internal OpenAI-compatible reverse proxy to free-tier AI providers
(Groq, Together AI, Google AI Studio). Holds the real provider API keys
so no other app on the zorc platform ever needs to see or handle one
directly.

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
  `GOOGLE_AI_STUDIO_API_KEY` from the environment directly, treating an
  unset one as "not configured yet" -- that provider's routes return a
  clean `503`, every other provider keeps working.
- `GET /providers` reports which ones currently have a real key set.
- Wiring a key in is `set_coolify_env_vars(coolify_uuid, {KEY: value})`
  followed by `redeploy()` -- a plain restart does NOT pick up a new env
  var, the container has to be recreated (same rule as a memory-limit
  change). Not yet a standalone zorc tool.
- `GROQ_API_KEY` is wired and confirmed working end-to-end (real
  `openai/gpt-oss-120b` chat completion through this gateway, 2026-08-23).
  `TOGETHER_API_KEY` / `GOOGLE_AI_STUDIO_API_KEY` are not set yet.

## Adding a provider

Add one entry to `PROVIDERS` in `main.py` (upstream base URL, the env var
name for its key, how the key gets attached to the request) -- no other
code changes needed, the proxy route itself is entirely table-driven.

## Google AI Studio note

`google`'s base URL points at Gemini's documented OpenAI-compatibility
endpoint. This has NOT been verified against a real key/live request yet
(no `GOOGLE_AI_STUDIO_API_KEY` configured as of this writing) -- confirm
it once that key is wired in, and adjust `PROVIDERS["google"]["base_url"]`
if it doesn't match.

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
