# floodsense-llm

A model gateway that speaks the two Meterless calls the FloodSense agents
make, and forwards them to an OpenAI-compatible upstream.

**This is not Nightshift.** Nightshift is the team's gateway, run per machine
on `:8088`, and the NVIDIA provider key lives only in its Providers tab —
never in an application environment. This service holds no provider key
either: it is wired to the platform's own gateway, which owns the keys and
fails over between providers.

It exists so the agents can run on a real model now, and be pointed at
Nightshift later by changing one environment variable.

Every response carries `X-Engine: floodsense-llm`, and the model that
actually answered comes back in `model`. The agents record that on the trace,
so a run names its model instead of claiming one.

## The calls

```
GET  /v1/models
POST /v1/chat/completions
Authorization: Bearer $LLM_GATEWAY_KEY
meterless-app: floodsense-eu
meterless-scope: user
```

The body is passed through unchanged except `chat_template_kwargs`, which is
a Nemotron switch another runtime may reject and which costs nothing to drop.
A 400 mentioning `response_format` is passed through rather than masked: the
agents already retry without it when a runtime has no JSON mode.

The upstream substitutes its own model for the one requested, so `MODEL_FAST`
and `MODEL_DEEP` are labels for the lane rather than a choice of model. What
answered is in the response and on the trace.

## Running it

| variable | |
|---|---|
| `LLM_BASE_URL` | upstream, wired by the platform (`ai: true`) |
| `LLM_API_KEY` | upstream key, wired by the platform |
| `LLM_GATEWAY_KEY` | the bearer token our own callers present (required) |
| `LLM_TIMEOUT_S` | default 180 |

```bash
LLM_BASE_URL=... LLM_API_KEY=... LLM_GATEWAY_KEY=local-dev-key PORT=8102 python -m app
```

Then point the agents at it:

```bash
METERLESS_BASE_URL=http://127.0.0.1:8102/v1 METERLESS_RUNTIME_TOKEN=local-dev-key \
  python -m floodsense_agents
```

`GET /ready` calls the upstream's `/models` and returns 503 if it cannot be
reached. `GET /health` never calls upstream, because that answer decides
whether the node is marked healthy.

## Switching to the real Nightshift

Point `METERLESS_BASE_URL` at `http://host.docker.internal:8088/v1` (or the
host URL `http://127.0.0.1:8088/v1`), set `METERLESS_RUNTIME_TOKEN`, and set
`MODEL_FAST` / `MODEL_DEEP` to the routing modes Nightshift expects. Nothing
else changes.

Every model call in the agents has a deterministic fallback, so when neither
this gateway nor Nightshift can be reached, a run still completes — on the
deterministic path, and the trace says which agents took it.
