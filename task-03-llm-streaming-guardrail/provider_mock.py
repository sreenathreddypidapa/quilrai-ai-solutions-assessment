"""Mock OpenAI-style SSE streaming provider. No API key or external network."""
import asyncio
import json
from aiohttp import web

async def generate(request):
    data = await request.json()
    if not isinstance(data, dict) or not isinstance(data.get('prompt'), str):
        return web.json_response({'error':'prompt must be a string'}, status=400)
    # Prompt is echoed as simulated model output to enable deterministic tests.
    text = data['prompt']
    response = web.StreamResponse(status=200, headers={
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        'X-Accel-Buffering': 'no',
    })
    await response.prepare(request)
    for i in range(0, len(text), 3):
        event = {'choices':[{'delta':{'content':text[i:i+3]}}]}
        await response.write(('data: '+json.dumps(event)+'\n\n').encode())
        await asyncio.sleep(0.001)
    await response.write(b'data: [DONE]\n\n')
    await response.write_eof()
    return response

app = web.Application()
app.router.add_post('/v1/chat/completions', generate)
if __name__ == '__main__':
    web.run_app(app, host='127.0.0.1', port=8101)
