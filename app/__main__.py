"""A Meterless-compatible model gateway for FloodSense.

This is not Nightshift. Nightshift is the team's gateway, run per machine,
and the NVIDIA provider key lives only in its Providers tab -- never in an
app environment, which is why nothing here holds one either.

It speaks the two calls the agents make -- GET /v1/models and
POST /v1/chat/completions, with the meterless-app and meterless-scope
headers -- and forwards them to whatever OpenAI-compatible upstream it is
given. So the agents can run on a real model today and be pointed at
Nightshift later by changing one environment variable.

It says what it is: X-Engine on every response. The model that actually
answered comes back in `model`, and the agents record it on the trace, so a
run names its model rather than claiming one.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

ENGINE = "floodsense-llm"

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format='{"ts":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}',
    stream=sys.stdout,
)
log = logging.getLogger("floodsense.llm")


def _require(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise RuntimeError(f"{name} is not set; refusing to start")
    return v


# Wired by the platform when app.yaml declares ai: true. We never hold a real
# provider key: the upstream gateway owns those and fails over between them.
UPSTREAM = _require("LLM_BASE_URL").rstrip("/")
UPSTREAM_KEY = _require("LLM_API_KEY")
API_KEY = _require("LLM_GATEWAY_KEY")  # what our own callers present
TIMEOUT_S = float(os.environ.get("LLM_TIMEOUT_S", "180"))
GIT_SHA = os.environ.get("GIT_SHA", "unknown")
BUILT_AT = os.environ.get("BUILT_AT", "unknown")

app = FastAPI(
    title="FloodSense LLM",
    description="A Meterless-compatible model gateway over an OpenAI-compatible upstream.",
    version="1.0.0",
)


@app.middleware("http")
async def name_the_engine(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Engine"] = ENGINE
    return response


def _check(authorization: str | None) -> None:
    import secrets

    token = (authorization or "").removeprefix("Bearer ").strip()
    if not token or not secrets.compare_digest(token, API_KEY):
        raise HTTPException(status_code=401, detail="bad or missing bearer token")


def _upstream_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {UPSTREAM_KEY}", "content-type": "application/json"}


# --- platform contract ----------------------------------------------------


@app.get("/health")
def health() -> dict:
    """Must not call upstream: this answer decides whether the node is healthy."""
    return {"status": "ok"}


@app.get("/ready")
def ready() -> JSONResponse:
    try:
        r = httpx.get(f"{UPSTREAM}/models", headers=_upstream_headers(), timeout=15)
        r.raise_for_status()
        n = len(r.json().get("data", []))
    except Exception as e:
        log.warning("upstream not ready: %s", str(e)[:200])
        return JSONResponse({"status": "not ready", "reason": str(e)[:200]}, status_code=503)
    return JSONResponse({"status": "ok", "upstream_models": n})


@app.get("/version")
def version() -> dict:
    return {"sha": GIT_SHA, "built": BUILT_AT, "engine": ENGINE}


# --- the Meterless calls the agents make ----------------------------------


@app.get("/v1/models")
def models(authorization: str | None = Header(default=None)) -> dict:
    _check(authorization)
    try:
        r = httpx.get(f"{UPSTREAM}/models", headers=_upstream_headers(), timeout=30)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"upstream: {str(e)[:200]}") from e


@app.post("/v1/chat/completions")
def chat(
    body: dict,
    authorization: str | None = Header(default=None),
    meterless_app: str | None = Header(default=None),
    meterless_scope: str | None = Header(default=None),
) -> dict:
    _check(authorization)
    if not body.get("messages"):
        raise HTTPException(status_code=400, detail="messages is required")

    # Nemotron's switch for suppressing a reasoning preamble. Another runtime
    # may reject an unknown field, and dropping it costs nothing.
    out = {k: v for k, v in body.items() if k != "chat_template_kwargs"}

    t0 = datetime.now(timezone.utc)
    try:
        r = httpx.post(
            f"{UPSTREAM}/chat/completions",
            json=out,
            headers=_upstream_headers(),
            timeout=TIMEOUT_S,
        )
        # The agents already retry without response_format when a runtime
        # rejects it, so pass that refusal through rather than masking it.
        if r.status_code == 400 and "response_format" in r.text:
            raise HTTPException(status_code=400, detail=r.text[:300])
        r.raise_for_status()
        js = r.json()
    except HTTPException:
        raise
    except httpx.HTTPError as e:
        log.warning("upstream failed app=%s: %s", meterless_app, str(e)[:200])
        raise HTTPException(status_code=502, detail=f"upstream: {str(e)[:200]}") from e

    took = (datetime.now(timezone.utc) - t0).total_seconds()
    log.info(
        "completion app=%s scope=%s asked=%s answered=%s %.1fs",
        meterless_app,
        meterless_scope,
        body.get("model"),
        js.get("model"),
        took,
    )
    return js


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
