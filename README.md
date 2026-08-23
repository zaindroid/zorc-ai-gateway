# zorc-ai-gateway

Internal OpenAI-compatible reverse proxy to free-tier AI providers
(Groq, Together AI, Google AI Studio). Holds the real provider API keys
so no other app on the zorc platform ever needs to see or handle one
directly.

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
- Wiring a key in later is a direct Coolify env-var update + restart
  against this app's UUID -- not yet a standalone zorc tool, done the
  same way the zbots memory-limit bump was: `set_coolify_env_vars` (or
  the Coolify UI) followed by a restart so the new value is picked up.

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
