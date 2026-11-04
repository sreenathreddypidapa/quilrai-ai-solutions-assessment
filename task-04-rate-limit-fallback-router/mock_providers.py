"""Two intentionally simple, local mock LLM HTTP providers for Task 4."""
from __future__ import annotations
import asyncio
import logging
import os
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def create_app(provider: str) -> web.Application:
    if provider not in ("primary", "backup"):
        raise ValueError("provider must be primary or backup")
    app = web.Application()
    app["calls"] = 0
    app["received_authorization"] = False

    async def generate(request: web.Request):
        app["calls"] += 1
        app["received_authorization"] = app["received_authorization"] or "Authorization" in request.headers
        payload = await request.json()
        prompt = payload.get("prompt", "")
        label = "primary" if provider == "primary" else "backup"
        if f"[{label}:429]" in prompt:
            return web.json_response({"details": "mock rate limit"}, status=429)
        if f"[{label}:500]" in prompt:
            return web.Response(text="SENSITIVE INTERNAL STACK TRACE", status=500)
        if f"[{label}:timeout]" in prompt:
            await asyncio.sleep(3.3 if label == "primary" else 6.0)
        if f"[{label}:invalid]" in prompt:
            return web.Response(text="SENSITIVE INTERNAL STACK TRACE", content_type="text/plain")
        if f"[{label}:badusage]" in prompt:
            return web.json_response({"output": f"{provider} answer", "usage": {"total_tokens": "bad"}})
        # This is mock usage for demonstrating reconciliation; real providers
        # should supply their tokenizer-accurate usage metadata.
        input_tokens = len(prompt.split())
        output_tokens = min(payload.get("max_tokens", 1), 4)
        return web.json_response({
            "output": f"{provider} generated: {prompt}",
            "usage": {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
        })

    async def metrics(_: web.Request):
        return web.json_response({"provider": provider, "requests_received": app["calls"], "received_authorization": app["received_authorization"]})

    app.router.add_post("/generate", generate)
    app.router.add_get("/_metrics", metrics)
    return app


if __name__ == "__main__":
    provider = os.environ.get("PROVIDER", "primary")
    port = int(os.environ.get("MOCK_PROVIDER_PORT", "8201" if provider == "primary" else "8202"))
    logging.info("Mock %s model listening on http://127.0.0.1:%s/generate", provider, port)
    web.run_app(create_app(provider), host="127.0.0.1", port=port, print=None)
