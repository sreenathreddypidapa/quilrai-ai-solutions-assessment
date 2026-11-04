"""Async streaming LLM gateway; SSE in, SSE out, bounded PII redaction."""
import json
import os
import aiohttp
from aiohttp import web
from guardrail import StreamRedactor

PROVIDER_URL = os.getenv('PROVIDER_URL', 'http://127.0.0.1:8101/v1/chat/completions')
MAX_BODY = 64 * 1024

def sse_delta(text):
    return ('data: '+json.dumps({'choices':[{'delta':{'content':text}}]})+'\n\n').encode()

async def generate(request):
    try:
        data = await request.json()
    except (ValueError, UnicodeDecodeError):
        return web.json_response({'error':'invalid JSON'}, status=400)
    if not isinstance(data, dict) or not isinstance(data.get('prompt'), str):
        return web.json_response({'error':'prompt must be a string'}, status=400)
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=30)
    session = aiohttp.ClientSession(timeout=timeout)
    try:
        upstream = await session.post(PROVIDER_URL, json=data)
    except (aiohttp.ClientError, TimeoutError):
        await session.close()
        return web.json_response({'error':'upstream unavailable'}, status=502)
    if upstream.status != 200:
        status = upstream.status
        upstream.release()
        await session.close()
        return web.json_response({'error':'upstream error'}, status=502 if status >= 500 else 400)

    response = web.StreamResponse(status=200, headers={
        'Content-Type':'text/event-stream', 'Cache-Control':'no-cache, no-transform',
        'X-Accel-Buffering':'no',
    })
    await response.prepare(request)
    redactor = StreamRedactor()
    # Parse upstream SSE frames incrementally; never collect the full response.
    # Bound upstream frame length to avoid unbounded buffering on malformed SSE.
    line_buffer = bytearray()
    try:
        async for chunk in upstream.content.iter_chunked(1024):
            for byte in chunk:
                if byte != 10:
                    line_buffer.append(byte)
                    if len(line_buffer) > 65536:
                        raise ValueError('upstream SSE line too long')
                    continue
                line = bytes(line_buffer).rstrip(b'\r')
                line_buffer.clear()
                if not line.startswith(b'data:'):
                    continue
                payload = line[5:].strip()
                if payload == b'[DONE]':
                    tail = redactor.finish()
                    if tail:
                        await response.write(sse_delta(tail))
                    await response.write(b'data: [DONE]\n\n')
                    await response.write_eof()
                    return response
                try:
                    event = json.loads(payload)
                    delta = event['choices'][0]['delta'].get('content', '')
                    if not isinstance(delta, str):
                        raise ValueError('invalid delta')
                except (ValueError, KeyError, IndexError, TypeError):
                    raise ValueError('invalid upstream SSE delta')
                safe = redactor.feed(delta)
                if safe:
                    await response.write(sse_delta(safe))
        # Provider terminated without [DONE]: flush only after complete SSE stream.
        if line_buffer:
            raise ValueError('incomplete upstream SSE line')
        tail = redactor.finish()
        if tail:
            await response.write(sse_delta(tail))
        await response.write(b'data: [DONE]\n\n')
        await response.write_eof()
    except (aiohttp.ClientError, ConnectionError, TimeoutError, ValueError) as exc:
        # Headers already sent; report stream error via SSE, not a new HTTP status.
        try:
            await response.write(('event: error\ndata: '+json.dumps({'error':str(exc)})+'\n\n').encode())
            await response.write_eof()
        except (ConnectionError, RuntimeError):
            pass
    finally:
        upstream.release()
        await session.close()
    return response

app = web.Application(client_max_size=MAX_BODY)
app.router.add_post('/v1/chat/completions', generate)
if __name__ == '__main__':
    web.run_app(app, host='127.0.0.1', port=8100)
