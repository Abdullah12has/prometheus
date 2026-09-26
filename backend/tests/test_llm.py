import asyncio
import json

import httpx
import pytest

from permetheus.llm import LanguageModel, ModelUnavailable


def test_unconfigured_model_is_an_explicit_error():
    with pytest.raises(ModelUnavailable, match='Configure'):
        asyncio.run(LanguageModel(None, None, None).complete([]))


def test_provider_stream_and_tool_request(monkeypatch):
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        if body.get('stream'):
            return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"Hello"}}]}\n\ndata: [DONE]\n\n')
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}]})

    client_type = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs))

    async def check():
        model = LanguageModel('https://model.example/v1', 'test-only', 'fast')
        tools = [{'type': 'function', 'function': {'name': 'lookup', 'parameters': {'type': 'object'}}}]
        assert (await model.complete([{'role': 'user', 'content': 'Find a company'}], tools=tools))['content'] == 'ok'
        assert ''.join([part async for part in model.stream([])]) == 'Hello'

    asyncio.run(check())
    assert all(path == '/v1/chat/completions' for path, _ in requests)
    assert requests[0][1]['tools'][0]['function']['name'] == 'lookup'


def test_provider_error_does_not_expose_response_or_key(monkeypatch):
    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(401, text='secret provider diagnostic'))
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: client_type(transport=transport, **kwargs))
    with pytest.raises(ModelUnavailable) as error:
        asyncio.run(LanguageModel('https://model.example', 'test-secret', 'fast').complete([]))
    assert 'secret' not in str(error.value)
